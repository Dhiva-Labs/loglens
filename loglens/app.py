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
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.widgets import Footer, Input, RichLog, Sparkline, Static

from loglens import __version__
from loglens.buffer import LineBuffer
from loglens.parsers import Level, LogLine, parser_for_file
from loglens.tailer import MultiTailer, read_tail

MAX_RENDER_LINES = 1000
# Ceiling RichLog itself is told to retain (its own automatic trimming of
# old lines). _rendered_seqs is capped to match, so it never claims a seq is
# still on screen after RichLog has quietly dropped it from the front.
MAX_LOG_LINES = 2000
PRELOAD_LINES = 50
POLL_INTERVAL = 0.1
HISTOGRAM_INTERVAL = 1.0
HISTOGRAM_BUCKET_SECONDS = 60
HISTOGRAM_BUCKETS = 15

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

    #filter-input, #search-input {
        display: none;
    }

    #filter-input.filter-invalid, #search-input.filter-invalid {
        border: tall $error;
    }

    #histogram {
        height: 3;
        display: none;
        background: $panel;
        padding: 0 1;
    }

    #histogram-label {
        height: 1;
        color: $text-muted;
    }

    #histogram-sparkline {
        height: 2;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("q", "quit", "Quit", show=True),
        Binding("/", "focus_filter", "Filter", show=True),
        Binding("s", "focus_search", "Search", show=True),
        Binding("n", "search_next", "Next match", show=False),
        Binding("N", "search_prev", "Prev match", show=False),
        Binding("e", "toggle_errors_only", "Errors", show=True),
        Binding("p", "toggle_pause", "Pause", show=True),
        Binding("t", "toggle_histogram", "Histogram", show=True),
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

        # Search state, driven by the s / n / N keybindings. Unlike the
        # filter, search is not live: _search_pattern only changes when the
        # search input is submitted. _search_matches holds the seqs of every
        # entry that passed the view filters (_matches) and the pattern, at
        # the moment the search ran, ascending oldest-first; _search_index
        # points into it at the "current" match.
        self._search_pattern: re.Pattern[str] | None = None
        self._search_matches: list[int] = []
        self._search_index: int = 0
        self._search_invalid: bool = False

        # Seqs of the entries currently held by the log widget, in render
        # order - lets a search jump tell in O(len) whether its target is
        # already on screen (pure scroll) or needs a re-render.
        self._rendered_seqs: list[int] = []

        self._histogram_visible: bool = False

    # -- composition / lifecycle ------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static(self._status_renderable(), id="status-bar")
        with Vertical(id="histogram"):
            yield Static(self._histogram_label_text([]), id="histogram-label")
            yield Sparkline([], id="histogram-sparkline")
        yield RichLog(
            wrap=False,
            highlight=False,
            markup=False,
            auto_scroll=True,
            max_lines=MAX_LOG_LINES,
            id="tail-log",
        )
        yield Input(
            placeholder="regex filter — Enter keeps, Esc clears",
            id="filter-input",
        )
        yield Input(
            placeholder="search buffer — Enter jumps, Esc cancels",
            id="search-input",
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
        self.set_interval(HISTOGRAM_INTERVAL, self._refresh_histogram)

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
            for seq, line, effective_level in new_entries:
                if self._matches(line, effective_level):
                    log.write(self._format_line(line, seq))
                    self._rendered_seqs.append(seq)
            if len(self._rendered_seqs) > MAX_LOG_LINES:
                del self._rendered_seqs[: len(self._rendered_seqs) - MAX_LOG_LINES]
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

    def _filtered_snapshot(self) -> tuple[list[tuple[int, LogLine]], int]:
        """One buffer.since(0) snapshot, oldest first, narrowed to the current
        view filters (errors_only / filter_pattern), plus its top seq.

        Deliberately doesn't call buffer.view(): that call and a follow-up
        read of buffer.total_appended are two separate lock acquisitions, so
        anything the tailer thread appends in between would be excluded from
        the render yet already covered by the new watermark - silently
        dropped from every future _poll. Pulling the whole buffer via
        since(0) instead gets the entries and their top seq from the same
        locked snapshot, so a watermark derived from it always matches what
        was actually filtered, and filtering/limiting happens here instead.

        Shared by _refresh_view (tail render) and _render_window (a jump
        that misses the current render and needs a fresh centered window) so
        both filter from the exact same read.
        """
        entries = self.buffer.since(0)
        top = entries[-1][0] if entries else 0
        filtered = [
            (seq, line)
            for seq, line, effective_level in entries
            if self._matches(line, effective_level)
        ]
        return filtered, top

    def _render_lines(self, entries: list[tuple[int, LogLine]]) -> None:
        """Clear and rewrite the log from `entries` (oldest first), tracking
        exactly which seqs ended up on screen in `_rendered_seqs`.
        """
        log = self.query_one("#tail-log", RichLog)
        log.clear()
        for seq, line in entries:
            log.write(self._format_line(line, seq))
        self._rendered_seqs = [seq for seq, _ in entries]

    def _refresh_view(self) -> None:
        """Clear and re-render the newest MAX_RENDER_LINES filtered lines."""
        filtered, top = self._filtered_snapshot()
        self._render_lines(filtered[-MAX_RENDER_LINES:])
        self._last_seq = top
        self._refresh_status()

    def _render_window(self, center_seq: int, *, force: bool = False) -> None:
        """Jump the log view to `center_seq`.

        If it's already part of what's currently rendered (and a fresh
        render isn't `force`d), this is a pure scroll: no re-render, no
        flicker - but also no change to which line carries the current-match
        emphasis, since that only gets (re)applied when _format_line runs
        again (see its docstring). Otherwise this re-renders a
        MAX_RENDER_LINES window of the current filtered snapshot, centered
        on the target, then scrolls to it there.

        `force=True` is used the moment a search's results are (re)computed:
        the target may already be part of `_rendered_seqs` from before the
        search pattern was set, in which case those lines were rendered
        without any highlighting at all and a scroll-only jump would leave
        them that way.
        """
        log = self.query_one("#tail-log", RichLog)

        if not force and center_seq in self._rendered_seqs:
            log.scroll_to(y=self._rendered_seqs.index(center_seq), animate=False)
            return

        filtered, top = self._filtered_snapshot()
        seqs = [seq for seq, _ in filtered]
        try:
            target_index = seqs.index(center_seq)
        except ValueError:
            # The target fell out of the buffer (evicted) or the current
            # filters between when the match was found and now - nothing
            # sensible to jump to.
            return

        half = MAX_RENDER_LINES // 2
        start = max(0, min(target_index - half, len(filtered) - MAX_RENDER_LINES))
        end = min(len(filtered), start + MAX_RENDER_LINES)

        self._render_lines(filtered[start:end])
        self._last_seq = top
        self._refresh_status()
        log.scroll_to(y=target_index - start, animate=False)

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
        if event.input.id != "filter-input":
            return
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
        if event.input.id == "filter-input":
            self._hide_filter_input()
        elif event.input.id == "search-input":
            self._submit_search(event.input)

    def action_clear_filter(self) -> None:
        """Escape, context-sensitive:

        - search input showing -> hide+cancel it only, active search results
          (if any) are left untouched.
        - else a search is active (has been run, matches or not) -> clear
          the search, but stay paused - the filter is untouched.
        - else -> the original behavior: clear the filter outright, whether
          or not the filter input is showing.
        """
        search_input = self.query_one("#search-input", Input)
        if search_input.display:
            self._hide_search_input()
            return

        if self._search_pattern is not None:
            self._clear_search()
            return

        # Doesn't need to touch the filter input's own text -
        # action_focus_filter always repopulates it from self.filter_pattern
        # (now None, so empty) the next time the input is revealed.
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
            # Resuming live-tail would fight a frozen search view (auto
            # scroll pulling the log away from wherever n/N last jumped to),
            # so resuming always drops any active search first.
            self._reset_search()
            log.auto_scroll = True
            self._refresh_view()
            log.scroll_end(animate=False)
        self._refresh_status()

    # -- search (s, n, N, Enter, Escape) ---------------------------------------

    def action_focus_search(self) -> None:
        """Reveal the search input, pre-filled with the active pattern, and focus it."""
        input_widget = self.query_one("#search-input", Input)
        pattern = self._search_pattern
        input_widget.value = pattern.pattern if pattern is not None else ""
        self._search_invalid = False
        input_widget.remove_class("filter-invalid")
        input_widget.display = True
        input_widget.focus()
        self._refresh_status()

    def _submit_search(self, input_widget: Input) -> None:
        """Enter in the search input: compile once (not live) and, if valid,
        compute matches and jump to the newest one.
        """
        value = input_widget.value

        if value == "":
            self._clear_search()
            self._hide_search_input()
            return

        try:
            pattern = re.compile(value)
        except re.error:
            # Same treatment as the filter: keep whatever search was already
            # active, leave the input open with just the indicator lit so
            # the text can be fixed without losing the previous results.
            self._search_invalid = True
            input_widget.add_class("filter-invalid")
            self._refresh_status()
            return

        self._search_pattern = pattern
        self._search_invalid = False
        input_widget.remove_class("filter-invalid")
        self._run_search()
        self._hide_search_input()

    def _run_search(self) -> None:
        """Search the current filtered view for the active pattern.

        One buffer.since(0) snapshot, narrowed by the current view filters
        (the same _matches used for live rendering) and then by the search
        pattern - so search only ever surfaces lines the user could actually
        see. Matches are stored ascending by seq; the newest one starts as
        current.
        """
        assert self._search_pattern is not None
        pattern = self._search_pattern
        entries = self.buffer.since(0)
        self._search_matches = [
            seq
            for seq, line, effective_level in entries
            if self._matches(line, effective_level) and pattern.search(line.raw) is not None
        ]
        self._search_index = len(self._search_matches) - 1

        if not self.paused:
            self.action_toggle_pause()

        if not self._search_matches:
            self._refresh_status()
            return

        self._render_window(self._search_matches[self._search_index], force=True)
        self.query_one("#tail-log", RichLog).focus()
        self._refresh_status()

    def action_search_next(self) -> None:
        """n: walk to the next older match, wrapping to the newest."""
        if not self._search_matches:
            return
        self._search_index = (self._search_index - 1) % len(self._search_matches)
        self._render_window(self._search_matches[self._search_index])
        self._refresh_status()

    def action_search_prev(self) -> None:
        """N: walk back toward the next newer match, wrapping to the oldest."""
        if not self._search_matches:
            return
        self._search_index = (self._search_index + 1) % len(self._search_matches)
        self._render_window(self._search_matches[self._search_index])
        self._refresh_status()

    def _reset_search(self) -> None:
        self._search_pattern = None
        self._search_matches = []
        self._search_index = 0
        self._search_invalid = False
        self.query_one("#search-input", Input).remove_class("filter-invalid")

    def _clear_search(self) -> None:
        """Escape while a search is active: drop pattern/matches/highlighting,
        but stay paused - only the search state changes.
        """
        self._reset_search()
        self._refresh_view()
        self.query_one("#tail-log", RichLog).focus()

    def _hide_search_input(self) -> None:
        input_widget = self.query_one("#search-input", Input)
        input_widget.display = False
        self._search_invalid = False
        input_widget.remove_class("filter-invalid")
        self._refresh_status()
        self.query_one("#tail-log", RichLog).focus()

    # -- error histogram (t) ----------------------------------------------------

    def action_toggle_histogram(self) -> None:
        self._histogram_visible = not self._histogram_visible
        self.query_one("#histogram", Vertical).display = self._histogram_visible
        if self._histogram_visible:
            self._refresh_histogram()

    def _refresh_histogram(self) -> None:
        if not self._histogram_visible:
            return
        try:
            counts = self.buffer.error_counts(
                bucket_seconds=HISTOGRAM_BUCKET_SECONDS, buckets=HISTOGRAM_BUCKETS
            )
            self.query_one("#histogram-sparkline", Sparkline).data = counts
            self.query_one("#histogram-label", Static).update(self._histogram_label_text(counts))
        except NoMatches:
            return

    def _histogram_label_text(self, counts: list[int]) -> str:
        peak = max(counts) if counts else 0
        minutes = HISTOGRAM_BUCKET_SECONDS * HISTOGRAM_BUCKETS // 60
        return f"errors/min · {minutes}m · peak {peak}"

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

        if self._search_invalid:
            text.append("  ")
            text.append("invalid search regex", style="bold red")
        elif self._search_pattern is not None:
            text.append("  ")
            pattern_text = self._search_pattern.pattern
            if self._search_matches:
                # Position counted from oldest=1: _search_matches is stored
                # ascending, so the index itself already is that count.
                position = self._search_index + 1
                total = len(self._search_matches)
                text.append(f"?{pattern_text}? match {position}/{total}", style="bold yellow")
            else:
                text.append(f"?{pattern_text}? no matches", style="bold yellow")

        return text

    def _format_line(self, line: LogLine, seq: int | None = None) -> Text:
        """Render one buffered line.

        `seq` is only used for search highlighting: when a search is active,
        every substring matching the pattern is styled "black on yellow"
        (applied as spans on top of the base level style, via Text.stylize -
        chosen over Text.highlight_regex so matching runs only against
        line.raw, never against the badge/timestamp prefix already in
        `text`). If `seq` is the current match's seq, its whole raw segment
        additionally gets "bold" layered on top, so the eye has something to
        land on beyond just "one of the yellow highlights".
        """
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
        raw_start = len(text)
        text.append(line.raw, style=style or None)

        if self._search_pattern is not None:
            for match in self._search_pattern.finditer(line.raw):
                if match.start() == match.end():
                    continue
                text.stylize("black on yellow", raw_start + match.start(), raw_start + match.end())
            if (
                seq is not None
                and self._search_matches
                and self._search_matches[self._search_index] == seq
            ):
                text.stylize("bold", raw_start, raw_start + len(line.raw))

        return text
