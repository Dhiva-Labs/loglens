"""The Textual application: a merged, live-tailing view across log files.

Layout is a status bar, a scrolling log view, and a footer. On mount the app
preloads recent context from each file, then hands the paths to a
MultiTailer whose callback runs on a background thread and only ever touches
the thread-safe LineBuffer. The app itself polls that buffer on a timer to
pull in whatever is new and render it - no cross-thread Textual calls.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import ClassVar

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.widgets import Footer, Input, RichLog, Static

from loglens import __version__
from loglens.buffer import LineBuffer
from loglens.parsers import Level, LogLine, parser_for_file
from loglens.tailer import MultiTailer, read_tail

MAX_RENDER_LINES = 1000
PRELOAD_LINES = 50
POLL_INTERVAL = 0.1

# Cycled by file order to give each tailed source a stable, distinct color.
BADGE_COLORS: tuple[str, ...] = ("cyan", "magenta", "green", "yellow", "blue", "bright_red")

# Style applied to a rendered raw line, keyed by its level. Level.INFO and
# None (unclassified) both render with no extra style.
_LEVEL_STYLES: dict[Level | None, str] = {
    Level.ERROR: "bold red",
    Level.WARN: "yellow",
    Level.DEBUG: "dim",
    Level.INFO: "",
    None: "",
}


def _badge_labels(paths: Sequence[str]) -> dict[str, str]:
    """Map each path to a display badge: basename, disambiguated with the
    parent directory on collision, then padded so every badge is equal width.
    """
    names = [Path(p).name for p in paths]
    name_counts: dict[str, int] = {}
    for name in names:
        name_counts[name] = name_counts.get(name, 0) + 1

    labels: dict[str, str] = {}
    for path in paths:
        name = Path(path).name
        if name_counts[name] > 1:
            parent = Path(path).parent.name
            labels[path] = f"{parent}/{name}" if parent else name
        else:
            labels[path] = name

    width = max((len(label) for label in labels.values()), default=0)
    return {path: label.ljust(width) for path, label in labels.items()}


class LogLensApp(App[None]):
    """Tails one or more log files and renders them as one merged, colored view."""

    CSS = """
    Screen {
        layout: vertical;
    }

    #status-bar {
        height: 1;
        background: $panel;
        color: $text;
        padding: 0 1;
    }

    #tail-log {
        height: 1fr;
        background: $surface;
    }

    #filter-input {
        display: none;
    }

    #filter-input.filter-invalid {
        border: tall $error;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("q", "quit", "Quit", show=True),
        Binding("/", "focus_filter", "Filter", show=True),
        Binding("e", "toggle_errors_only", "Errors", show=True),
        Binding("p", "toggle_pause", "Pause", show=True),
        Binding("escape", "clear_filter", "Clear filter", show=False, priority=True),
    ]

    def __init__(self, paths: Sequence[str]) -> None:
        super().__init__()
        self.paths: list[str] = list(paths)
        self.buffer = LineBuffer()

        self._badges: dict[str, str] = _badge_labels(self.paths)
        self._colors: dict[str, str] = {
            path: BADGE_COLORS[index % len(BADGE_COLORS)] for index, path in enumerate(self.paths)
        }
        self._parsers: dict[str, ModuleType] = {}
        self._tailer: MultiTailer | None = None
        self._last_seq = 0

        # View state: driven by the / filter input, and the e / p keybindings.
        # The render path funnels through these so setting them and calling
        # _refresh_view() is the whole story.
        self.filter_pattern: re.Pattern[str] | None = None
        self.errors_only: bool = False
        self.paused: bool = False
        # True while the filter input holds text that fails re.compile. The
        # previously applied filter_pattern is left untouched while this is
        # set - only the status bar and the input's border reflect it.
        self._filter_invalid: bool = False

    # -- composition / lifecycle ------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static(self._status_renderable(), id="status-bar")
        yield RichLog(
            wrap=False,
            highlight=False,
            markup=False,
            auto_scroll=True,
            max_lines=2000,
            id="tail-log",
        )
        yield Input(
            placeholder="regex filter — Enter keeps, Esc clears",
            id="filter-input",
        )
        yield Footer()

    def on_mount(self) -> None:
        for path in self.paths:
            self._parsers[path] = parser_for_file(path)

        for path in self.paths:
            parser = self._parsers[path]
            raw_lines = read_tail(path, max_lines=PRELOAD_LINES)
            self.buffer.extend(parser.parse(raw, path) for raw in raw_lines)

        self._refresh_view()

        self._tailer = MultiTailer(self.paths, on_lines=self._on_lines)
        self._tailer.start()

        self.set_interval(POLL_INTERVAL, self._poll)

        # Scrolling (arrow keys, PageUp/PageDown) should work without an
        # extra click, both now and whenever the filter input hides again.
        self.query_one("#tail-log", RichLog).focus()

    def on_unmount(self) -> None:
        if self._tailer is not None:
            self._tailer.stop()
            self._tailer = None

    # -- tailer callback (background thread - buffer writes only) ----------

    def _on_lines(self, source: str, raw_lines: list[str]) -> None:
        parser = self._parsers.get(source)
        if parser is None:
            return
        self.buffer.extend(parser.parse(raw, source) for raw in raw_lines)

    # -- app-thread polling --------------------------------------------------

    def _poll(self) -> None:
        # This timer keeps firing every POLL_INTERVAL for the app's whole
        # life, including the tail end of shutdown - unlike action handlers,
        # which only run in response to a delivered key press and so can't
        # land while the screen is mid-teardown. A tick that lands in that
        # narrow window finds status-bar/tail-log already unmounted; treat
        # that as "nothing to update this tick" rather than an error.
        try:
            self._refresh_status()

            if self.paused:
                return

            new_entries = self.buffer.since(self._last_seq)
            if not new_entries:
                return
            self._last_seq = new_entries[-1][0]

            log = self.query_one("#tail-log", RichLog)
            for _seq, line, effective_level in new_entries:
                if self._matches(line, effective_level):
                    log.write(self._format_line(line))
        except NoMatches:
            return

    def _matches(self, line: LogLine, effective_level: Level | None) -> bool:
        """Whether a single fresh entry should be written to the live view.

        `effective_level` is the level continuation lines inherit from their
        parent (see LineBuffer.since/view) - checking it instead of
        `line.level` is what keeps traceback continuations of an ERROR line
        visible under errors-only, matching what a full _refresh_view would
        show.
        """
        if self.errors_only and effective_level != Level.ERROR:
            return False
        if self.filter_pattern is not None and self.filter_pattern.search(line.raw) is None:
            return False
        return True

    def _refresh_view(self) -> None:
        """Clear and re-render the tail of the buffer under the current filters.

        Deliberately doesn't call buffer.view(): that call and a follow-up
        read of buffer.total_appended are two separate lock acquisitions, so
        anything the tailer thread appends in between would be excluded from
        the render yet already covered by the new watermark - silently
        dropped from every future _poll. Pulling the whole buffer via
        since(0) instead gets the entries and their top seq from the same
        locked snapshot, so the watermark always matches what was rendered,
        and filtering/limiting happens here instead.
        """
        log = self.query_one("#tail-log", RichLog)
        log.clear()

        entries = self.buffer.since(0)
        top = entries[-1][0] if entries else 0

        matched: list[tuple[int, LogLine]] = []
        for seq, line, effective_level in reversed(entries):
            if self._matches(line, effective_level):
                matched.append((seq, line))
                if len(matched) >= MAX_RENDER_LINES:
                    break
        matched.reverse()

        for _seq, line in matched:
            log.write(self._format_line(line))

        self._last_seq = top
        self._refresh_status()

    def _refresh_status(self) -> None:
        self.query_one("#status-bar", Static).update(self._status_renderable())

    # -- filter input (/, Enter, Escape) --------------------------------------

    def action_focus_filter(self) -> None:
        """Reveal the filter input, pre-filled with the active pattern, and focus it."""
        input_widget = self.query_one("#filter-input", Input)
        input_widget.value = self.filter_pattern.pattern if self.filter_pattern is not None else ""
        self._filter_invalid = False
        input_widget.remove_class("filter-invalid")
        input_widget.display = True
        input_widget.focus()
        self._refresh_status()

    def on_input_changed(self, event: Input.Changed) -> None:
        input_widget = event.input
        value = event.value

        if value == "":
            self.filter_pattern = None
            self._filter_invalid = False
            input_widget.remove_class("filter-invalid")
            self._refresh_view()
            return

        try:
            pattern = re.compile(value)
        except re.error:
            # Keep whatever filter_pattern was already applied - only the
            # input's own indicator reflects the bad text underneath it.
            self._filter_invalid = True
            input_widget.add_class("filter-invalid")
            self._refresh_status()
            return

        self.filter_pattern = pattern
        self._filter_invalid = False
        input_widget.remove_class("filter-invalid")
        self._refresh_view()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._hide_filter_input()

    def action_clear_filter(self) -> None:
        """Escape: clear the filter outright, whether or not the input is showing.

        Doesn't need to touch the input's own text - action_focus_filter
        always repopulates it from self.filter_pattern (now None, so empty)
        the next time the input is revealed.
        """
        input_widget = self.query_one("#filter-input", Input)
        self.filter_pattern = None
        self._filter_invalid = False
        input_widget.remove_class("filter-invalid")
        input_widget.display = False
        self._refresh_view()
        self.query_one("#tail-log", RichLog).focus()

    def _hide_filter_input(self) -> None:
        input_widget = self.query_one("#filter-input", Input)
        input_widget.display = False
        self._filter_invalid = False
        input_widget.remove_class("filter-invalid")
        self._refresh_status()
        self.query_one("#tail-log", RichLog).focus()

    # -- errors-only (e) and pause/resume (p) ---------------------------------

    def action_toggle_errors_only(self) -> None:
        self.errors_only = not self.errors_only
        self._refresh_view()

    def action_toggle_pause(self) -> None:
        self.paused = not self.paused
        log = self.query_one("#tail-log", RichLog)
        if self.paused:
            log.auto_scroll = False
        else:
            log.auto_scroll = True
            self._refresh_view()
            log.scroll_end(animate=False)
        self._refresh_status()

    # -- rendering -----------------------------------------------------------

    def _status_renderable(self) -> Text:
        text = Text()
        text.append(f"LogLens v{__version__}", style="bold")
        text.append("  ")
        for path in self.paths:
            badge = self._badges[path].strip()
            color = self._colors[path]
            text.append(badge, style=f"bold {color}")
            text.append("  ")
        text.append(f"lines: {self.buffer.total_appended}", style="dim")

        if self.paused:
            text.append("  ")
            text.append("PAUSED", style="reverse yellow")
        if self.errors_only:
            text.append("  ")
            text.append("ERRORS", style="bold red")
        if self._filter_invalid:
            text.append("  ")
            text.append("invalid regex", style="bold red")
        elif self.filter_pattern is not None:
            text.append("  ")
            text.append(f"/{self.filter_pattern.pattern}/", style="bold cyan")

        return text

    def _format_line(self, line: LogLine) -> Text:
        badge = self._badges.get(line.source, line.source)
        color = self._colors.get(line.source, "white")

        if line.timestamp is not None:
            timestamp = line.timestamp.strftime("%H:%M:%S")
        else:
            timestamp = datetime.fromtimestamp(line.arrival).strftime("%H:%M:%S")

        style = "dim" if line.continuation else _LEVEL_STYLES.get(line.level, "")

        text = Text()
        text.append(f"[{badge}]", style=f"bold {color}")
        text.append(" ")
        text.append(timestamp, style="dim")
        text.append(" ")
        text.append(line.raw, style=style or None)
        return text
