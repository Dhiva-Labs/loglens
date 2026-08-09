"""Tests for the Textual application: mounting, preload, live tail, and styling."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from pathlib import Path

from textual.widgets import Input, OptionList, RichLog, Sparkline, Static

from loglens.app import MAX_RENDER_LINES, ClusterScreen, LogLensApp, _badge_labels
from loglens.buffer import LineBuffer
from loglens.parsers import Level, LogLine


async def _wait_for(
    condition: Callable[[], bool], pilot, timeout: float = 5.0, interval: float = 0.2
) -> None:
    """Poll condition(), pausing the pilot each cycle, until true or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await pilot.pause()
        if condition():
            return
        await asyncio.sleep(interval)
    if not condition():
        raise AssertionError(f"condition not met within {timeout}s")


def _rendered_text(rich_log: RichLog) -> str:
    return "\n".join(strip.text for strip in rich_log.lines)


def _append_error_with_trace(app: LogLensApp, source: str, *, row: int) -> None:
    """Seed one ERROR head plus a traceback of continuation lines, all from
    the same template - only `row` (embedded in a frame's line number and in
    the exception message) varies between calls. cluster.py masks numbers
    and drops line numbers from frame identity, so any number of calls with
    different `row` values merge into a single cluster.
    """
    head = "unhandled exception while processing request"
    app.buffer.append(LogLine(source=source, raw=head, message=head, level=Level.ERROR))
    for raw in (
        "Traceback (most recent call last):",
        f'  File "app/handler.py", line {row}, in handle',
        f"ValueError: bad thing happened id={row}",
    ):
        app.buffer.append(LogLine(source=source, raw=raw, message=raw, continuation=True))


# -- badge helper (direct unit test) --------------------------------------------


def test_badge_labels_pads_to_equal_width() -> None:
    labels = _badge_labels(["/var/log/app.log", "/var/log/nginx-access.log"])
    values = list(labels.values())
    assert len({len(value) for value in values}) == 1
    assert labels["/var/log/app.log"].strip() == "app.log"
    assert labels["/var/log/nginx-access.log"].strip() == "nginx-access.log"


def test_badge_labels_disambiguates_same_basename_with_parent_dir() -> None:
    labels = _badge_labels(["/srv/api/app.log", "/srv/worker/app.log"])
    assert labels["/srv/api/app.log"].strip() == "api/app.log"
    assert labels["/srv/worker/app.log"].strip() == "worker/app.log"
    assert len(labels["/srv/api/app.log"]) == len(labels["/srv/worker/app.log"])


def test_badge_labels_unique_basenames_not_disambiguated() -> None:
    labels = _badge_labels(["/a/one.log", "/b/two.log"])
    assert labels["/a/one.log"].strip() == "one.log"
    assert labels["/b/two.log"].strip() == "two.log"


# -- mounting / status bar ------------------------------------------------------


async def test_app_mounts_with_log_and_status(tmp_path: Path) -> None:
    file_a = tmp_path / "a.log"
    file_b = tmp_path / "b.log"
    file_a.write_text("hello from a\n")
    file_b.write_text("hello from b\n")

    app = LogLensApp([str(file_a), str(file_b)])
    async with app.run_test() as pilot:
        await pilot.pause()

        rich_log = app.query_one("#tail-log", RichLog)
        assert rich_log is not None

        status = app.query_one("#status-bar", Static)
        status_text = status.content.plain
        assert "a.log" in status_text
        assert "b.log" in status_text


# -- preload ----------------------------------------------------------------------


async def test_preload_shows_existing_content_after_mount(tmp_path: Path) -> None:
    file_a = tmp_path / "a.log"
    file_a.write_text("preload line one\npreload line two\n")
    file_b = tmp_path / "b.log"
    file_b.write_text("other file line\n")

    app = LogLensApp([str(file_a), str(file_b)])
    async with app.run_test() as pilot:
        await pilot.pause()

        rich_log = app.query_one("#tail-log", RichLog)
        rendered = _rendered_text(rich_log)
        assert "preload line one" in rendered
        assert "preload line two" in rendered
        assert "other file line" in rendered


# -- live append --------------------------------------------------------------------


async def test_live_append_appears_within_a_few_seconds(tmp_path: Path) -> None:
    file_a = tmp_path / "a.log"
    file_a.write_text("initial content\n")
    file_b = tmp_path / "b.log"
    file_b.write_text("other initial\n")

    app = LogLensApp([str(file_a), str(file_b)])
    async with app.run_test() as pilot:
        await pilot.pause()
        before_count = app.buffer.total_appended

        with file_a.open("a") as handle:
            handle.write("LIVE_APPEND_MARKER hello\n")
            handle.flush()

        rich_log = app.query_one("#tail-log", RichLog)
        await _wait_for(lambda: "LIVE_APPEND_MARKER" in _rendered_text(rich_log), pilot)

        assert app.buffer.total_appended > before_count
        assert "[a.log" in _rendered_text(rich_log)


# -- level styling ------------------------------------------------------------------


def test_format_line_styles_error_bold_red() -> None:
    app = LogLensApp(["a.log"])
    line = LogLine(
        source="a.log", raw="boom", message="boom", level=Level.ERROR, arrival=time.time()
    )
    text = app._format_line(line)
    assert "boom" in text.plain
    assert any(span.style == "bold red" for span in text.spans)


def test_format_line_styles_warn_yellow() -> None:
    app = LogLensApp(["a.log"])
    line = LogLine(
        source="a.log", raw="careful", message="careful", level=Level.WARN, arrival=time.time()
    )
    text = app._format_line(line)
    assert any(span.style == "yellow" for span in text.spans)


def test_format_line_styles_debug_dim() -> None:
    app = LogLensApp(["a.log"])
    line = LogLine(
        source="a.log", raw="details", message="details", level=Level.DEBUG, arrival=time.time()
    )
    text = app._format_line(line)
    assert any(span.style == "dim" for span in text.spans)


def test_format_line_styles_continuation_dim() -> None:
    app = LogLensApp(["a.log"])
    line = LogLine(
        source="a.log",
        raw="  frame",
        message="  frame",
        continuation=True,
        arrival=time.time(),
    )
    text = app._format_line(line)
    assert any(span.style == "dim" for span in text.spans)


def test_format_line_info_has_no_raw_line_style() -> None:
    app = LogLensApp(["a.log"])
    line = LogLine(
        source="a.log", raw="all good", message="all good", level=Level.INFO, arrival=time.time()
    )
    text = app._format_line(line)
    # Only the badge and timestamp carry style; INFO gets no third span.
    assert len(text.spans) == 2


# -- q quits ------------------------------------------------------------------------


async def test_q_quits(tmp_path: Path) -> None:
    file_a = tmp_path / "a.log"
    file_a.write_text("line\n")

    app = LogLensApp([str(file_a)])
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()

    assert app.return_code == 0


# -- _matches (unit-style, no keybindings yet) ---------------------------------------


def test_matches_errors_only_filters_non_error_lines() -> None:
    app = LogLensApp(["a.log"])
    app.errors_only = True
    error_line = LogLine(source="a.log", raw="boom", message="boom", level=Level.ERROR)
    info_line = LogLine(source="a.log", raw="fine", message="fine", level=Level.INFO)
    assert app._matches(error_line, Level.ERROR) is True
    assert app._matches(info_line, Level.INFO) is False


def test_matches_errors_only_uses_effective_level_not_line_level() -> None:
    # A continuation line carries no level of its own (None) - errors-only
    # has to go by the effective level LineBuffer computed for it, not
    # line.level, or traceback continuations of an ERROR line would vanish
    # from the live view even though a full _refresh_view would keep them.
    app = LogLensApp(["a.log"])
    app.errors_only = True
    continuation = LogLine(
        source="a.log", raw="  File x.py", message="  File x.py", continuation=True
    )
    assert continuation.level is None
    assert app._matches(continuation, Level.ERROR) is True
    assert app._matches(continuation, Level.INFO) is False


def test_matches_filter_pattern_requires_search_hit() -> None:
    app = LogLensApp(["a.log"])
    app.filter_pattern = re.compile("needle")
    hit = LogLine(source="a.log", raw="a needle here", message="")
    miss = LogLine(source="a.log", raw="nothing here", message="")
    assert app._matches(hit, None) is True
    assert app._matches(miss, None) is False


def test_matches_combines_errors_only_and_pattern() -> None:
    app = LogLensApp(["a.log"])
    app.errors_only = True
    app.filter_pattern = re.compile("disk")
    matching = LogLine(source="a.log", raw="disk full", message="", level=Level.ERROR)
    wrong_level = LogLine(source="a.log", raw="disk ok", message="", level=Level.INFO)
    wrong_pattern = LogLine(source="a.log", raw="cpu spike", message="", level=Level.ERROR)
    assert app._matches(matching, Level.ERROR) is True
    assert app._matches(wrong_level, Level.INFO) is False
    assert app._matches(wrong_pattern, Level.ERROR) is False


def test_matches_no_filters_accepts_everything() -> None:
    app = LogLensApp(["a.log"])
    line = LogLine(source="a.log", raw="anything at all", message="")
    assert app._matches(line, None) is True


# -- / filter input -----------------------------------------------------------------


async def test_slash_reveals_focuses_and_narrows_live_then_enter_persists(tmp_path: Path) -> None:
    file_a = tmp_path / "a.log"
    file_a.write_text("seed\n")

    app = LogLensApp([str(file_a)])
    async with app.run_test() as pilot:
        await pilot.pause()

        input_widget = app.query_one("#filter-input", Input)
        assert input_widget.display is False

        await pilot.press("/")
        await pilot.pause()
        assert input_widget.display is True
        assert app.focused is input_widget

        app.buffer.append(
            LogLine(source=str(file_a), raw="alpha needle here", message="alpha needle here")
        )
        app.buffer.append(
            LogLine(source=str(file_a), raw="beta no match at all", message="beta no match at all")
        )

        rich_log = app.query_one("#tail-log", RichLog)
        await pilot.press(*"needle")
        await pilot.pause()

        await _wait_for(lambda: "alpha needle here" in _rendered_text(rich_log), pilot)
        assert "beta no match at all" not in _rendered_text(rich_log)

        await pilot.press("enter")
        await pilot.pause()

        assert input_widget.display is False
        assert app.focused is rich_log

        status = app.query_one("#status-bar", Static)
        assert "/needle/" in status.content.plain

        # The filter stays live while the input is hidden.
        app.buffer.append(LogLine(source=str(file_a), raw="gamma unrelated", message="x"))
        app.buffer.append(LogLine(source=str(file_a), raw="delta needle again", message="x"))
        await _wait_for(lambda: "delta needle again" in _rendered_text(rich_log), pilot)
        assert "gamma unrelated" not in _rendered_text(rich_log)


# -- Escape ---------------------------------------------------------------------------


async def test_escape_while_input_focused_clears_filter_and_restores_view(
    tmp_path: Path,
) -> None:
    file_a = tmp_path / "a.log"
    file_a.write_text("seed\n")

    app = LogLensApp([str(file_a)])
    async with app.run_test() as pilot:
        await pilot.pause()

        app.buffer.append(LogLine(source=str(file_a), raw="keep me visible", message="x"))
        app.buffer.append(LogLine(source=str(file_a), raw="drop me maybe", message="x"))

        rich_log = app.query_one("#tail-log", RichLog)
        input_widget = app.query_one("#filter-input", Input)

        await pilot.press("/")
        await pilot.pause()
        await pilot.press(*"keep")
        await pilot.pause()
        await _wait_for(lambda: "keep me visible" in _rendered_text(rich_log), pilot)
        assert "drop me maybe" not in _rendered_text(rich_log)

        await pilot.press("escape")
        await pilot.pause()

        assert input_widget.display is False
        assert app.filter_pattern is None
        assert app.focused is rich_log
        await _wait_for(lambda: "drop me maybe" in _rendered_text(rich_log), pilot)


async def test_escape_with_filter_active_but_input_hidden_clears_it(tmp_path: Path) -> None:
    file_a = tmp_path / "a.log"
    file_a.write_text("seed\n")

    app = LogLensApp([str(file_a)])
    async with app.run_test() as pilot:
        await pilot.pause()

        app.buffer.append(LogLine(source=str(file_a), raw="keep me visible", message="x"))
        app.buffer.append(LogLine(source=str(file_a), raw="drop me maybe", message="x"))

        rich_log = app.query_one("#tail-log", RichLog)
        input_widget = app.query_one("#filter-input", Input)

        await pilot.press("/")
        await pilot.pause()
        await pilot.press(*"keep")
        await pilot.press("enter")
        await pilot.pause()

        assert input_widget.display is False
        assert app.filter_pattern is not None

        await pilot.press("escape")
        await pilot.pause()

        assert app.filter_pattern is None
        await _wait_for(lambda: "drop me maybe" in _rendered_text(rich_log), pilot)


# -- invalid regex --------------------------------------------------------------------


async def test_invalid_regex_keeps_previous_filter_and_recovers(tmp_path: Path) -> None:
    file_a = tmp_path / "a.log"
    file_a.write_text("seed\n")

    app = LogLensApp([str(file_a)])
    async with app.run_test() as pilot:
        await pilot.pause()

        app.buffer.append(LogLine(source=str(file_a), raw="alpha needle", message="x"))
        app.buffer.append(LogLine(source=str(file_a), raw="beta other", message="x"))

        rich_log = app.query_one("#tail-log", RichLog)
        input_widget = app.query_one("#filter-input", Input)
        status = app.query_one("#status-bar", Static)

        await pilot.press("/")
        await pilot.pause()
        await pilot.press(*"needle")
        await pilot.pause()
        await _wait_for(lambda: "alpha needle" in _rendered_text(rich_log), pilot)
        assert "beta other" not in _rendered_text(rich_log)

        # "needle(" is an unterminated group - invalid.
        await pilot.press("(")
        await pilot.pause()

        assert input_widget.has_class("filter-invalid")
        assert "invalid regex" in status.content.plain
        assert app.filter_pattern is not None
        assert app.filter_pattern.pattern == "needle"
        # The view keeps showing what the last valid pattern matched.
        assert "alpha needle" in _rendered_text(rich_log)
        assert "beta other" not in _rendered_text(rich_log)

        # Backspacing the "(" recovers a valid pattern.
        await pilot.press("backspace")
        await pilot.pause()

        assert not input_widget.has_class("filter-invalid")
        assert "invalid regex" not in status.content.plain
        assert app.filter_pattern is not None
        assert app.filter_pattern.pattern == "needle"


# -- e: errors-only -------------------------------------------------------------------


async def test_errors_only_keeps_continuation_and_drops_other_sources(tmp_path: Path) -> None:
    file_a = tmp_path / "a.log"
    file_b = tmp_path / "b.log"
    file_a.write_text("seed a\n")
    file_b.write_text("seed b\n")

    app = LogLensApp([str(file_a), str(file_b)])
    async with app.run_test() as pilot:
        await pilot.pause()

        app.buffer.append(
            LogLine(
                source=str(file_a),
                raw="Traceback (most recent call last)",
                message="x",
                level=Level.ERROR,
            )
        )
        app.buffer.append(
            LogLine(source=str(file_a), raw="  File x.py, line 1", message="x", continuation=True)
        )
        app.buffer.append(
            LogLine(source=str(file_b), raw="just some info", message="x", level=Level.INFO)
        )

        rich_log = app.query_one("#tail-log", RichLog)
        status = app.query_one("#status-bar", Static)
        await _wait_for(lambda: "just some info" in _rendered_text(rich_log), pilot)

        await pilot.press("e")
        await pilot.pause()

        await _wait_for(
            lambda: (
                "just some info" not in _rendered_text(rich_log)
                and "Traceback" in _rendered_text(rich_log)
                and "File x.py, line 1" in _rendered_text(rich_log)
            ),
            pilot,
        )
        assert "ERRORS" in status.content.plain

        await pilot.press("e")
        await pilot.pause()

        await _wait_for(lambda: "just some info" in _rendered_text(rich_log), pilot)
        assert "ERRORS" not in status.content.plain


# -- p: pause/resume ------------------------------------------------------------------


async def test_pause_freezes_render_then_resume_shows_missed_lines(tmp_path: Path) -> None:
    file_a = tmp_path / "a.log"
    file_a.write_text("seed\n")

    app = LogLensApp([str(file_a)])
    async with app.run_test() as pilot:
        await pilot.pause()

        await pilot.press("p")
        await pilot.pause()

        status = app.query_one("#status-bar", Static)
        assert "PAUSED" in status.content.plain

        before = app.buffer.total_appended
        app.buffer.append(LogLine(source=str(file_a), raw="MISSED_WHILE_PAUSED", message="x"))

        await asyncio.sleep(1.0)
        await pilot.pause()

        assert app.buffer.total_appended > before
        rich_log = app.query_one("#tail-log", RichLog)
        assert "MISSED_WHILE_PAUSED" not in _rendered_text(rich_log)

        await pilot.press("p")
        await pilot.pause()

        await _wait_for(lambda: "MISSED_WHILE_PAUSED" in _rendered_text(rich_log), pilot)
        status = app.query_one("#status-bar", Static)
        assert "PAUSED" not in status.content.plain


# -- combined: filter + errors-only ----------------------------------------------------


async def test_filter_and_errors_only_combine(tmp_path: Path) -> None:
    file_a = tmp_path / "a.log"
    file_a.write_text("seed\n")

    app = LogLensApp([str(file_a)])
    async with app.run_test() as pilot:
        await pilot.pause()

        app.buffer.append(
            LogLine(source=str(file_a), raw="disk full error", message="x", level=Level.ERROR)
        )
        app.buffer.append(
            LogLine(source=str(file_a), raw="disk ok info", message="x", level=Level.INFO)
        )
        app.buffer.append(
            LogLine(source=str(file_a), raw="cpu spike error", message="x", level=Level.ERROR)
        )

        rich_log = app.query_one("#tail-log", RichLog)
        await _wait_for(lambda: "cpu spike error" in _rendered_text(rich_log), pilot)

        await pilot.press("e")
        await pilot.pause()
        await pilot.press("/")
        await pilot.pause()
        await pilot.press(*"disk")
        await pilot.pause()

        await _wait_for(lambda: "disk full error" in _rendered_text(rich_log), pilot)
        rendered = _rendered_text(rich_log)
        assert "disk ok info" not in rendered
        assert "cpu spike error" not in rendered

        await pilot.press("enter")
        await pilot.pause()

        status = app.query_one("#status-bar", Static)
        assert "ERRORS" in status.content.plain
        assert "/disk/" in status.content.plain


# -- _refresh_view / _last_seq watermark consistency ------------------------------------


async def test_refresh_view_watermark_matches_what_it_rendered(tmp_path: Path) -> None:
    # _refresh_view must derive its _last_seq watermark from the very same
    # snapshot it rendered from - not from a second, later read of the
    # buffer's total count. Otherwise anything appended in between the two
    # reads is skipped by the rendered pass *and* by every future _poll,
    # since the watermark has already moved past it.
    file_a = tmp_path / "a.log"
    file_a.write_text("seed\n")

    app = LogLensApp([str(file_a)])
    async with app.run_test() as pilot:
        await pilot.pause()

        app.buffer.append(LogLine(source=str(file_a), raw="before one", message="x"))
        app.buffer.append(LogLine(source=str(file_a), raw="before two", message="x"))

        app._refresh_view()

        newest_seq = app.buffer.snapshot()[-1][0]
        assert app._last_seq == newest_seq

        rich_log = app.query_one("#tail-log", RichLog)
        assert "before one" in _rendered_text(rich_log)
        assert "before two" in _rendered_text(rich_log)

        # Lines that arrive after _refresh_view's snapshot must still be
        # picked up by the next _poll tick, not silently swallowed by a
        # watermark that had already moved past them.
        app.buffer.append(LogLine(source=str(file_a), raw="after one", message="x"))
        app.buffer.append(LogLine(source=str(file_a), raw="after two", message="x"))

        await _wait_for(lambda: "after two" in _rendered_text(rich_log), pilot)
        assert "after one" in _rendered_text(rich_log)


async def test_refresh_view_respects_filters_from_full_snapshot(tmp_path: Path) -> None:
    # _refresh_view now filters in-app over since(0) instead of delegating
    # to buffer.view() - make sure that path still honors errors_only and
    # filter_pattern (including continuation attachment) the same way.
    file_a = tmp_path / "a.log"
    file_a.write_text("seed\n")

    app = LogLensApp([str(file_a)])
    async with app.run_test() as pilot:
        await pilot.pause()

        app.buffer.append(
            LogLine(source=str(file_a), raw="Traceback", message="x", level=Level.ERROR)
        )
        app.buffer.append(
            LogLine(source=str(file_a), raw="  frame one", message="x", continuation=True)
        )
        app.buffer.append(
            LogLine(source=str(file_a), raw="unrelated info", message="x", level=Level.INFO)
        )

        app.errors_only = True
        app._refresh_view()

        rich_log = app.query_one("#tail-log", RichLog)
        rendered = _rendered_text(rich_log)
        assert "Traceback" in rendered
        assert "frame one" in rendered
        assert "unrelated info" not in rendered


# -- s: search -----------------------------------------------------------------------


async def test_s_reveals_search_input_and_enter_finds_newest_match_current(
    tmp_path: Path,
) -> None:
    file_a = tmp_path / "a.log"
    file_a.write_text("seed\n")

    app = LogLensApp([str(file_a)])
    async with app.run_test() as pilot:
        await pilot.pause()

        input_widget = app.query_one("#search-input", Input)
        assert input_widget.display is False

        app.buffer.append(LogLine(source=str(file_a), raw="alpha needle one", message="x"))
        app.buffer.append(LogLine(source=str(file_a), raw="beta no match", message="x"))
        app.buffer.append(LogLine(source=str(file_a), raw="gamma needle two", message="x"))

        rich_log = app.query_one("#tail-log", RichLog)
        await _wait_for(lambda: "gamma needle two" in _rendered_text(rich_log), pilot)

        await pilot.press("s")
        await pilot.pause()
        assert input_widget.display is True
        assert app.focused is input_widget

        await pilot.press(*"needle")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert input_widget.display is False
        assert app.focused is rich_log
        assert app.paused is True
        assert len(app._search_matches) == 2

        # The newest match ("gamma needle two", appended last) is current.
        assert app._search_index == len(app._search_matches) - 1
        newest_seq = app._search_matches[-1]
        current_line = app.buffer.get(newest_seq)
        assert current_line is not None
        assert current_line.raw == "gamma needle two"

        status = app.query_one("#status-bar", Static)
        assert "match 2/2" in status.content.plain

        # The current match's raw segment carries both the search highlight
        # and the extra current-line emphasis.
        text = app._format_line(current_line, newest_seq)
        assert any(span.style == "black on yellow" for span in text.spans)
        assert any(span.style == "bold" for span in text.spans)


async def test_n_and_N_walk_and_wrap_reretrieving_off_window_matches(tmp_path: Path) -> None:
    file_a = tmp_path / "a.log"
    file_a.write_text("seed\n")

    app = LogLensApp([str(file_a)])
    async with app.run_test() as pilot:
        await pilot.pause()

        # Freeze the live view first: _poll streams new lines incrementally
        # with no MAX_RENDER_LINES cap of its own (only RichLog's much
        # larger max_lines applies), so a bulk append while live would just
        # render everything. Pausing first and rendering with one explicit
        # _refresh_view() call instead reproduces the capped-tail shape a
        # long-running session would actually have - e.g. right after
        # reopening a paused view or toggling a filter with a big backlog.
        await pilot.press("p")
        await pilot.pause()
        assert app.paused is True

        # A match near the very start, a large run of filler past
        # MAX_RENDER_LINES, then a second match at the end - the first
        # match is guaranteed to fall outside the default tail render.
        app.buffer.append(LogLine(source=str(file_a), raw="needle FIRST", message="x"))
        for i in range(1200):
            app.buffer.append(LogLine(source=str(file_a), raw=f"filler {i}", message="x"))
        app.buffer.append(LogLine(source=str(file_a), raw="needle LAST", message="x"))
        app._refresh_view()

        rich_log = app.query_one("#tail-log", RichLog)
        assert "needle LAST" in _rendered_text(rich_log)
        assert "needle FIRST" not in _rendered_text(rich_log)

        await pilot.press("s")
        await pilot.pause()
        await pilot.press(*"needle")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert len(app._search_matches) == 2
        assert app._search_index == 1
        status = app.query_one("#status-bar", Static)
        assert "match 2/2" in status.content.plain

        # n: next OLDER match - "needle FIRST", off the currently rendered
        # window, forcing a re-render centered on it.
        await pilot.press("n")
        await pilot.pause()

        assert app._search_index == 0
        assert "needle FIRST" in _rendered_text(rich_log)
        status = app.query_one("#status-bar", Static)
        assert "match 1/2" in status.content.plain

        # n again wraps back around to the newest match.
        await pilot.press("n")
        await pilot.pause()
        assert app._search_index == 1
        status = app.query_one("#status-bar", Static)
        assert "match 2/2" in status.content.plain

        # N: back toward newer, wrapping from newest to oldest.
        await pilot.press("N")
        await pilot.pause()
        assert app._search_index == 0
        status = app.query_one("#status-bar", Static)
        assert "match 1/2" in status.content.plain


async def test_search_respects_active_errors_only_filter(tmp_path: Path) -> None:
    file_a = tmp_path / "a.log"
    file_a.write_text("seed\n")

    app = LogLensApp([str(file_a)])
    async with app.run_test() as pilot:
        await pilot.pause()

        app.buffer.append(
            LogLine(source=str(file_a), raw="disk needle error", message="x", level=Level.ERROR)
        )
        app.buffer.append(
            LogLine(source=str(file_a), raw="disk needle info", message="x", level=Level.INFO)
        )

        rich_log = app.query_one("#tail-log", RichLog)
        await _wait_for(lambda: "disk needle info" in _rendered_text(rich_log), pilot)

        await pilot.press("e")
        await pilot.pause()

        await pilot.press("s")
        await pilot.pause()
        await pilot.press(*"needle")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        # Only the ERROR line is a match - the INFO line matches the
        # pattern but is excluded by errors-only, so search never sees it.
        assert len(app._search_matches) == 1
        matched_line = app.buffer.get(app._search_matches[0])
        assert matched_line is not None
        assert matched_line.level == Level.ERROR


async def test_invalid_search_regex_keeps_previous_search_and_indicator(tmp_path: Path) -> None:
    file_a = tmp_path / "a.log"
    file_a.write_text("seed\n")

    app = LogLensApp([str(file_a)])
    async with app.run_test() as pilot:
        await pilot.pause()

        app.buffer.append(LogLine(source=str(file_a), raw="alpha needle", message="x"))
        app.buffer.append(LogLine(source=str(file_a), raw="beta other", message="x"))

        rich_log = app.query_one("#tail-log", RichLog)
        await _wait_for(lambda: "beta other" in _rendered_text(rich_log), pilot)

        input_widget = app.query_one("#search-input", Input)
        status = app.query_one("#status-bar", Static)

        await pilot.press("s")
        await pilot.pause()
        await pilot.press(*"needle")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert app._search_pattern is not None
        assert app._search_pattern.pattern == "needle"
        previous_matches = list(app._search_matches)
        assert previous_matches

        # Reopen (prefilled with "needle"), make it invalid, submit it.
        await pilot.press("s")
        await pilot.pause()
        assert input_widget.value == "needle"
        await pilot.press("(")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        # No crash, previous search preserved, indicator lit, input stays
        # open (unlike the filter, submitting invalid search text doesn't
        # hide the input - there's nothing live applied yet to hide behind).
        assert input_widget.display is True
        assert input_widget.has_class("filter-invalid")
        assert "invalid search regex" in status.content.plain
        assert app._search_pattern is not None
        assert app._search_pattern.pattern == "needle"
        assert app._search_matches == previous_matches

        # Backspacing recovers and can be resubmitted cleanly.
        await pilot.press("backspace")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert input_widget.display is False
        assert not input_widget.has_class("filter-invalid")
        assert "invalid search regex" not in status.content.plain


async def test_escape_while_search_input_open_only_hides_it(tmp_path: Path) -> None:
    file_a = tmp_path / "a.log"
    file_a.write_text("seed\n")

    app = LogLensApp([str(file_a)])
    async with app.run_test() as pilot:
        await pilot.pause()

        app.buffer.append(LogLine(source=str(file_a), raw="alpha needle", message="x"))
        app.buffer.append(LogLine(source=str(file_a), raw="alpha other", message="x"))

        rich_log = app.query_one("#tail-log", RichLog)
        await _wait_for(lambda: "alpha other" in _rendered_text(rich_log), pilot)

        await pilot.press("/")
        await pilot.pause()
        await pilot.press(*"alpha")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.filter_pattern is not None
        assert app.filter_pattern.pattern == "alpha"

        input_widget = app.query_one("#search-input", Input)
        await pilot.press("s")
        await pilot.pause()
        await pilot.press(*"needle")
        await pilot.pause()
        assert input_widget.display is True

        await pilot.press("escape")
        await pilot.pause()

        assert input_widget.display is False
        # Never submitted, so there was nothing to preserve or clear.
        assert app._search_pattern is None
        # The filter is untouched by this escape.
        assert app.filter_pattern is not None
        assert app.filter_pattern.pattern == "alpha"


async def test_escape_with_active_search_clears_it_but_stays_paused_and_keeps_filter(
    tmp_path: Path,
) -> None:
    file_a = tmp_path / "a.log"
    file_a.write_text("seed\n")

    app = LogLensApp([str(file_a)])
    async with app.run_test() as pilot:
        await pilot.pause()

        app.buffer.append(LogLine(source=str(file_a), raw="alpha needle", message="x"))
        app.buffer.append(LogLine(source=str(file_a), raw="alpha other", message="x"))

        rich_log = app.query_one("#tail-log", RichLog)
        await _wait_for(lambda: "alpha other" in _rendered_text(rich_log), pilot)

        await pilot.press("/")
        await pilot.pause()
        await pilot.press(*"alpha")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.filter_pattern is not None

        await pilot.press("s")
        await pilot.pause()
        await pilot.press(*"needle")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert app._search_pattern is not None
        assert app.paused is True

        await pilot.press("escape")
        await pilot.pause()

        assert app._search_pattern is None
        assert app._search_matches == []
        # Stays paused - only the search state changed.
        assert app.paused is True
        # The filter survives this escape untouched.
        assert app.filter_pattern is not None
        assert app.filter_pattern.pattern == "alpha"

        status = app.query_one("#status-bar", Static)
        assert "?" not in status.content.plain
        assert "/alpha/" in status.content.plain


async def test_resume_p_clears_search_state(tmp_path: Path) -> None:
    file_a = tmp_path / "a.log"
    file_a.write_text("seed\n")

    app = LogLensApp([str(file_a)])
    async with app.run_test() as pilot:
        await pilot.pause()

        app.buffer.append(LogLine(source=str(file_a), raw="alpha needle", message="x"))

        rich_log = app.query_one("#tail-log", RichLog)
        await _wait_for(lambda: "alpha needle" in _rendered_text(rich_log), pilot)

        await pilot.press("s")
        await pilot.pause()
        await pilot.press(*"needle")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert app._search_pattern is not None
        assert app.paused is True

        # p resumes (search auto-paused us) and must drop search state too,
        # since live-tailing and a frozen search view would otherwise fight
        # over where the log is scrolled.
        await pilot.press("p")
        await pilot.pause()

        assert app.paused is False
        assert app._search_pattern is None
        assert app._search_matches == []

        status = app.query_one("#status-bar", Static)
        assert "?" not in status.content.plain


def test_format_line_highlights_all_search_matches() -> None:
    app = LogLensApp(["a.log"])
    app._search_pattern = re.compile("needle")
    line = LogLine(source="a.log", raw="a needle then another needle", message="x")
    text = app._format_line(line)
    yellow_spans = [span for span in text.spans if span.style == "black on yellow"]
    assert len(yellow_spans) == 2


def test_format_line_bolds_only_the_current_match_line() -> None:
    app = LogLensApp(["a.log"])
    app._search_pattern = re.compile("needle")
    app._search_matches = [7]
    app._search_index = 0
    line = LogLine(source="a.log", raw="find the needle in here", message="x")

    current = app._format_line(line, seq=7)
    assert any(span.style == "black on yellow" for span in current.spans)
    assert any(span.style == "bold" for span in current.spans)

    other = app._format_line(line, seq=99)
    assert any(span.style == "black on yellow" for span in other.spans)
    assert not any(span.style == "bold" for span in other.spans)

    no_seq = app._format_line(line)
    assert any(span.style == "black on yellow" for span in no_seq.spans)
    assert not any(span.style == "bold" for span in no_seq.spans)


# -- t: error histogram ---------------------------------------------------------------


async def test_t_toggles_histogram_and_reflects_error_arrivals(tmp_path: Path) -> None:
    file_a = tmp_path / "a.log"
    file_a.write_text("seed\n")

    app = LogLensApp([str(file_a)])
    async with app.run_test() as pilot:
        await pilot.pause()

        container = app.query_one("#histogram")
        assert container.display is False

        now = time.time()
        for offset in (5, 30, 90):
            app.buffer.append(
                LogLine(
                    source=str(file_a),
                    raw="boom",
                    message="boom",
                    level=Level.ERROR,
                    arrival=now - offset,
                )
            )

        await pilot.press("t")
        await pilot.pause()

        assert container.display is True
        sparkline = app.query_one("#histogram-sparkline", Sparkline)
        assert sparkline.data is not None
        assert sum(sparkline.data) > 0

        label = app.query_one("#histogram-label", Static)
        assert "peak" in label.content

        await pilot.press("t")
        await pilot.pause()
        assert container.display is False


# -- c: error clustering panel ---------------------------------------------------------


async def test_c_opens_panel_with_merged_cluster_counts(tmp_path: Path) -> None:
    file_a = tmp_path / "a.log"
    file_a.write_text("seed\n")

    app = LogLensApp([str(file_a)])
    async with app.run_test() as pilot:
        await pilot.pause()

        for row in range(5):
            _append_error_with_trace(app, str(file_a), row=row)

        await pilot.press("c")
        await pilot.pause()

        assert isinstance(app.screen, ClusterScreen)
        options = app.screen.query_one("#cluster-options", OptionList)
        assert options.option_count == 1
        row_text = options.get_option_at_index(0).prompt.plain
        assert "[×5]" in row_text


async def test_c_with_empty_buffer_shows_empty_state(tmp_path: Path) -> None:
    file_a = tmp_path / "a.log"
    file_a.write_text("")

    app = LogLensApp([str(file_a)])
    async with app.run_test() as pilot:
        await pilot.pause()

        await pilot.press("c")
        await pilot.pause()

        assert isinstance(app.screen, ClusterScreen)
        empty = app.screen.query_one("#cluster-empty", Static)
        assert empty.display is True
        assert "no errors in buffer" in empty.content.plain


async def test_c_and_escape_close_panel_without_clearing_filter_underneath(
    tmp_path: Path,
) -> None:
    file_a = tmp_path / "a.log"
    file_a.write_text("seed\n")

    app = LogLensApp([str(file_a)])
    async with app.run_test() as pilot:
        await pilot.pause()

        await pilot.press("/")
        await pilot.pause()
        await pilot.press(*"needle")
        await pilot.press("enter")
        await pilot.pause()
        assert app.filter_pattern is not None

        await pilot.press("c")
        await pilot.pause()
        assert isinstance(app.screen, ClusterScreen)

        # "c" while open closes it - the filter set up before opening must
        # survive untouched.
        await pilot.press("c")
        await pilot.pause()
        assert app._cluster_screen is None
        assert not isinstance(app.screen, ClusterScreen)
        assert app.filter_pattern is not None
        assert app.filter_pattern.pattern == "needle"

        await pilot.press("c")
        await pilot.pause()
        assert isinstance(app.screen, ClusterScreen)

        # Escape while open must close the panel instead of falling through
        # to the filter-clearing action it's normally bound to.
        await pilot.press("escape")
        await pilot.pause()
        assert app._cluster_screen is None
        assert not isinstance(app.screen, ClusterScreen)
        assert app.filter_pattern is not None
        assert app.filter_pattern.pattern == "needle"


async def test_enter_on_row_pauses_and_jumps_to_newest_occurrence(tmp_path: Path) -> None:
    file_a = tmp_path / "a.log"
    file_a.write_text("seed\n")

    app = LogLensApp([str(file_a)])
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.paused is False

        # An older occurrence, a run of filler well past MAX_RENDER_LINES,
        # then a newer occurrence of the same template - the same shape the
        # off-window search test uses, so the jump exercises the real
        # windowing/centering math instead of trivially landing on-screen.
        _append_error_with_trace(app, str(file_a), row=1)
        for i in range(1200):
            app.buffer.append(LogLine(source=str(file_a), raw=f"filler {i}", message="x"))
        _append_error_with_trace(app, str(file_a), row=999)

        await pilot.press("c")
        await pilot.pause()

        options = app.screen.query_one("#cluster-options", OptionList)
        assert options.option_count == 1

        await pilot.press("enter")
        await pilot.pause()

        assert app._cluster_screen is None
        assert not isinstance(app.screen, ClusterScreen)
        assert app.paused is True

        rich_log = app.query_one("#tail-log", RichLog)
        assert "id=999" in _rendered_text(rich_log)


async def test_cluster_panel_refreshes_live_every_2s_when_buffer_grows(tmp_path: Path) -> None:
    file_a = tmp_path / "a.log"
    file_a.write_text("seed\n")

    app = LogLensApp([str(file_a)])
    async with app.run_test() as pilot:
        await pilot.pause()

        _append_error_with_trace(app, str(file_a), row=1)

        await pilot.press("c")
        await pilot.pause()

        options = app.screen.query_one("#cluster-options", OptionList)
        assert "[×1]" in options.get_option_at_index(0).prompt.plain

        _append_error_with_trace(app, str(file_a), row=2)
        _append_error_with_trace(app, str(file_a), row=3)

        await asyncio.sleep(2.1)
        await pilot.pause()

        options = app.screen.query_one("#cluster-options", OptionList)
        assert "[×3]" in options.get_option_at_index(0).prompt.plain


async def test_evicted_target_seq_shows_notice_without_crash(tmp_path: Path) -> None:
    file_a = tmp_path / "a.log"
    file_a.write_text("")

    app = LogLensApp([str(file_a)])
    app.buffer = LineBuffer(capacity=6)
    async with app.run_test() as pilot:
        await pilot.pause()

        _append_error_with_trace(app, str(file_a), row=1)

        await pilot.press("c")
        await pilot.pause()

        assert isinstance(app.screen, ClusterScreen)
        cluster_seq = app.screen._clusters[0].last_seq

        # Push enough through the tiny buffer, while the panel is still
        # open, to evict the line the selected row points at - without
        # waiting long enough for the panel's own 2s refresh to notice.
        for i in range(20):
            app.buffer.append(LogLine(source=str(file_a), raw=f"pressure {i}", message="x"))
        assert app.buffer.get(cluster_seq) is None

        await pilot.press("enter")
        await pilot.pause()

        assert app._cluster_screen is None
        assert not isinstance(app.screen, ClusterScreen)
        # No jump, no pause flip, no crash - just the notice.
        assert app.paused is False
        messages = [notification.message for notification in app._notifications]
        assert any("evicted" in message for message in messages)


async def test_cluster_panel_ignores_active_filter_and_errors_only(tmp_path: Path) -> None:
    file_a = tmp_path / "a.log"
    file_a.write_text("seed\n")

    app = LogLensApp([str(file_a)])
    async with app.run_test() as pilot:
        await pilot.pause()

        for row in range(3):
            _append_error_with_trace(app, str(file_a), row=row)

        # A filter that would exclude every one of the seeded lines from the
        # main view - clustering must not be affected by it.
        app.filter_pattern = re.compile("this-matches-nothing-at-all")
        app.errors_only = True

        await pilot.press("c")
        await pilot.pause()

        options = app.screen.query_one("#cluster-options", OptionList)
        assert options.option_count == 1
        assert "[×3]" in options.get_option_at_index(0).prompt.plain


# -- w: export filtered view -----------------------------------------------------------


async def test_w_reveals_export_input_prefilled_with_default_filename(tmp_path: Path) -> None:
    file_a = tmp_path / "a.log"
    file_a.write_text("seed\n")

    app = LogLensApp([str(file_a)])
    async with app.run_test() as pilot:
        await pilot.pause()

        input_widget = app.query_one("#export-input", Input)
        assert input_widget.display is False

        await pilot.press("w")
        await pilot.pause()

        assert input_widget.display is True
        assert app.focused is input_widget
        assert re.match(r"^loglens-export-\d{8}-\d{6}\.log$", input_widget.value)


async def test_export_with_no_filters_writes_every_buffered_line_in_order(
    tmp_path: Path,
) -> None:
    file_a = tmp_path / "a.log"
    file_a.write_text("seed\n")

    app = LogLensApp([str(file_a)])
    async with app.run_test() as pilot:
        await pilot.pause()

        app.buffer.append(LogLine(source=str(file_a), raw="line one", message="x"))
        app.buffer.append(LogLine(source=str(file_a), raw="line two", message="x"))
        app.buffer.append(LogLine(source=str(file_a), raw="line three", message="x"))

        rich_log = app.query_one("#tail-log", RichLog)
        await _wait_for(lambda: "line three" in _rendered_text(rich_log), pilot)

        expected = [line.raw for _, line in app.buffer.snapshot()]

        target = tmp_path / "out.log"
        input_widget = app.query_one("#export-input", Input)

        await pilot.press("w")
        await pilot.pause()
        input_widget.value = str(target)
        await pilot.press("enter")
        await pilot.pause()

        assert input_widget.display is False
        assert target.read_text().splitlines() == expected

        messages = [notification.message for notification in app._notifications]
        assert any(f"wrote {len(expected)} lines" in message for message in messages)


async def test_export_with_filters_writes_only_filtered_lines_including_continuations(
    tmp_path: Path,
) -> None:
    file_a = tmp_path / "a.log"
    file_a.write_text("seed\n")

    app = LogLensApp([str(file_a)])
    async with app.run_test() as pilot:
        await pilot.pause()

        app.buffer.append(
            LogLine(source=str(file_a), raw="disk full error", message="x", level=Level.ERROR)
        )
        app.buffer.append(
            LogLine(
                source=str(file_a),
                raw="  disk io failure trace",
                message="x",
                continuation=True,
            )
        )
        app.buffer.append(
            LogLine(source=str(file_a), raw="disk ok info", message="x", level=Level.INFO)
        )
        app.buffer.append(
            LogLine(source=str(file_a), raw="cpu spike error", message="x", level=Level.ERROR)
        )

        app.errors_only = True
        app.filter_pattern = re.compile("disk")
        app._refresh_view()

        target = tmp_path / "filtered.log"
        input_widget = app.query_one("#export-input", Input)

        await pilot.press("w")
        await pilot.pause()
        input_widget.value = str(target)
        await pilot.press("enter")
        await pilot.pause()

        assert target.read_text().splitlines() == [
            "disk full error",
            "  disk io failure trace",
        ]


async def test_export_to_existing_file_does_not_overwrite(tmp_path: Path) -> None:
    file_a = tmp_path / "a.log"
    file_a.write_text("seed\n")

    target = tmp_path / "existing.log"
    target.write_text("original content\n")

    app = LogLensApp([str(file_a)])
    async with app.run_test() as pilot:
        await pilot.pause()

        input_widget = app.query_one("#export-input", Input)
        await pilot.press("w")
        await pilot.pause()
        input_widget.value = str(target)
        await pilot.press("enter")
        await pilot.pause()

        assert target.read_text() == "original content\n"
        assert input_widget.display is True

        messages = [notification.message for notification in app._notifications]
        assert any("exists" in message and str(target) in message for message in messages)


async def test_export_to_unwritable_path_shows_error_without_crash(tmp_path: Path) -> None:
    file_a = tmp_path / "a.log"
    file_a.write_text("seed\n")

    app = LogLensApp([str(file_a)])
    async with app.run_test() as pilot:
        await pilot.pause()

        target = tmp_path / "no-such-dir" / "out.log"
        input_widget = app.query_one("#export-input", Input)

        await pilot.press("w")
        await pilot.pause()
        input_widget.value = str(target)
        await pilot.press("enter")
        await pilot.pause()

        assert not target.exists()
        assert input_widget.display is True

        messages = [notification.message for notification in app._notifications]
        assert messages


async def test_escape_cancels_export_input_without_touching_filter_or_chain(
    tmp_path: Path,
) -> None:
    file_a = tmp_path / "a.log"
    file_a.write_text("seed\n")

    app = LogLensApp([str(file_a)])
    async with app.run_test() as pilot:
        await pilot.pause()

        await pilot.press("/")
        await pilot.pause()
        await pilot.press(*"seed")
        await pilot.press("enter")
        await pilot.pause()
        assert app.filter_pattern is not None
        assert app.filter_pattern.pattern == "seed"

        export_input = app.query_one("#export-input", Input)
        await pilot.press("w")
        await pilot.pause()
        assert export_input.display is True

        await pilot.press("escape")
        await pilot.pause()

        assert export_input.display is False
        # The filter set up before opening export must survive untouched.
        assert app.filter_pattern is not None
        assert app.filter_pattern.pattern == "seed"

        # The rest of the escape chain still works afterwards: a plain
        # escape now falls through to clearing the still-active filter.
        await pilot.press("escape")
        await pilot.pause()
        assert app.filter_pattern is None


async def test_export_includes_lines_beyond_max_render_lines_cap(tmp_path: Path) -> None:
    file_a = tmp_path / "a.log"
    file_a.write_text("seed\n")

    app = LogLensApp([str(file_a)])
    async with app.run_test() as pilot:
        await pilot.pause()

        await pilot.press("p")
        await pilot.pause()

        total = MAX_RENDER_LINES + 500
        for i in range(total):
            app.buffer.append(LogLine(source=str(file_a), raw=f"bulk {i}", message="x"))
        app._refresh_view()

        rich_log = app.query_one("#tail-log", RichLog)
        assert "bulk 0" not in _rendered_text(rich_log)

        expected = [line.raw for _, line in app.buffer.snapshot()]

        target = tmp_path / "full.log"
        input_widget = app.query_one("#export-input", Input)

        await pilot.press("w")
        await pilot.pause()
        input_widget.value = str(target)
        await pilot.press("enter")
        await pilot.pause()

        assert target.read_text().splitlines() == expected
