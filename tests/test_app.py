"""Tests for the Textual application: mounting, preload, live tail, and styling."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from pathlib import Path

from textual.widgets import RichLog, Static

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
    assert app._matches(error_line) is True
    assert app._matches(info_line) is False


def test_matches_filter_pattern_requires_search_hit() -> None:
    app = LogLensApp(["a.log"])
    app.filter_pattern = re.compile("needle")
    hit = LogLine(source="a.log", raw="a needle here", message="")
    miss = LogLine(source="a.log", raw="nothing here", message="")
    assert app._matches(hit) is True
    assert app._matches(miss) is False


def test_matches_combines_errors_only_and_pattern() -> None:
    app = LogLensApp(["a.log"])
    app.errors_only = True
    app.filter_pattern = re.compile("disk")
    matching = LogLine(source="a.log", raw="disk full", message="", level=Level.ERROR)
    wrong_level = LogLine(source="a.log", raw="disk ok", message="", level=Level.INFO)
    wrong_pattern = LogLine(source="a.log", raw="cpu spike", message="", level=Level.ERROR)
    assert app._matches(matching) is True
    assert app._matches(wrong_level) is False
    assert app._matches(wrong_pattern) is False


def test_matches_no_filters_accepts_everything() -> None:
    app = LogLensApp(["a.log"])
    line = LogLine(source="a.log", raw="anything at all", message="")
    assert app._matches(line) is True
