"""Tests for the Textual application: mounting, preload, live tail, and styling."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from pathlib import Path

from textual.widgets import Input, RichLog, Static

from loglens.app import LogLensApp, _badge_labels
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
