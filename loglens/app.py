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
from textual.widgets import Footer, RichLog, Static

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
    """

    # Later milestones extend this list with filter/pause/search bindings.
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("q", "quit", "Quit", show=True),
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

        # View state: inert for this milestone, wired up to keybindings and
        # a filter input in M6/M7. The render path already funnels through
        # them so those milestones only need to flip the values and refresh.
        self.filter_pattern: re.Pattern[str] | None = None
        self.errors_only: bool = False
        self.paused: bool = False

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
        status = self.query_one("#status-bar", Static)
        status.update(self._status_renderable())

        if self.paused:
            return

        new_entries = self.buffer.since(self._last_seq)
        if not new_entries:
            return
        self._last_seq = new_entries[-1][0]

        log = self.query_one("#tail-log", RichLog)
        for _seq, line in new_entries:
            if self._matches(line):
                log.write(self._format_line(line))

    def _matches(self, line: LogLine) -> bool:
        """Whether a single fresh entry should be written to the live view."""
        if self.errors_only and line.level != Level.ERROR:
            return False
        if self.filter_pattern is not None and self.filter_pattern.search(line.raw) is None:
            return False
        return True

    def _refresh_view(self) -> None:
        """Clear and re-render the tail of the buffer under the current filters."""
        log = self.query_one("#tail-log", RichLog)
        log.clear()
        entries = self.buffer.view(
            pattern=self.filter_pattern,
            errors_only=self.errors_only,
            limit=MAX_RENDER_LINES,
        )
        for _seq, line in entries:
            log.write(self._format_line(line))
        self._last_seq = self.buffer.total_appended

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
