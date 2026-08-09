"""Tests for the multi-file tail engine."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

from loglens.tailer import MultiTailer, read_tail


def wait_for(condition: Callable[[], bool], timeout: float = 5.0, interval: float = 0.01) -> None:
    """Poll condition() every `interval` seconds until true; fails the test on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(interval)
    if not condition():
        raise AssertionError(f"condition not met within {timeout}s")


class Collector:
    """Thread-safe sink for on_lines callbacks, keeping arrival order."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.events: list[tuple[str, list[str]]] = []

    def __call__(self, source: str, lines: list[str]) -> None:
        with self._lock:
            self.events.append((source, list(lines)))

    def lines_for(self, source: str) -> list[str]:
        with self._lock:
            out: list[str] = []
            for src, lines in self.events:
                if src == source:
                    out.extend(lines)
            return out

    def all_lines(self) -> list[str]:
        with self._lock:
            out: list[str] = []
            for _src, lines in self.events:
                out.extend(lines)
            return out


# -- tail semantics -----------------------------------------------------------


def test_from_start_true_emits_existing_content(tmp_path: Path) -> None:
    path = tmp_path / "a.log"
    path.write_text("first\nsecond\n")

    collector = Collector()
    tailer = MultiTailer(
        [str(path)], collector, poll_interval=0.02, from_start=True, use_watchdog=False
    )
    tailer.start()
    try:
        wait_for(lambda: collector.lines_for(str(path)) == ["first", "second"])
    finally:
        tailer.stop()


def test_default_seeks_to_end_skips_preexisting(tmp_path: Path) -> None:
    path = tmp_path / "a.log"
    path.write_text("old1\nold2\n")

    collector = Collector()
    tailer = MultiTailer([str(path)], collector, poll_interval=0.02, use_watchdog=False)
    tailer.start()
    try:
        time.sleep(0.1)
        assert collector.all_lines() == []

        with open(path, "a") as f:
            f.write("new1\n")
        wait_for(lambda: collector.lines_for(str(path)) == ["new1"])
    finally:
        tailer.stop()


# -- appends --------------------------------------------------------------


def test_append_accumulates_in_order(tmp_path: Path) -> None:
    path = tmp_path / "a.log"
    path.write_text("")

    collector = Collector()
    tailer = MultiTailer([str(path)], collector, poll_interval=0.02, use_watchdog=False)
    tailer.start()
    try:
        with open(path, "a") as f:
            f.write("one\n")
        wait_for(lambda: collector.lines_for(str(path)) == ["one"])

        with open(path, "a") as f:
            f.write("two\nthree\n")
        wait_for(lambda: collector.lines_for(str(path)) == ["one", "two", "three"])
    finally:
        tailer.stop()


# -- rotation ---------------------------------------------------------------


def test_truncate_rotation_reads_from_zero(tmp_path: Path) -> None:
    path = tmp_path / "a.log"
    path.write_text("old-line\n")

    collector = Collector()
    tailer = MultiTailer([str(path)], collector, poll_interval=0.02, use_watchdog=False)
    tailer.start()
    try:
        with open(path, "r+b") as f:
            f.truncate(0)
        with open(path, "ab") as f:
            f.write(b"fresh\n")
        wait_for(lambda: collector.lines_for(str(path)) == ["fresh"])
    finally:
        tailer.stop()


def test_rename_recreate_rotation(tmp_path: Path) -> None:
    path = tmp_path / "a.log"
    path.write_text("old-line\n")

    collector = Collector()
    tailer = MultiTailer([str(path)], collector, poll_interval=0.02, use_watchdog=False)
    tailer.start()
    try:
        os.rename(str(path), str(tmp_path / "a.log.1"))
        with open(path, "wb") as f:
            f.write(b"new-line\n")
        wait_for(lambda: collector.lines_for(str(path)) == ["new-line"])
    finally:
        tailer.stop()


def test_deleted_file_then_recreated(tmp_path: Path) -> None:
    path = tmp_path / "a.log"
    path.write_text("")

    collector = Collector()
    tailer = MultiTailer([str(path)], collector, poll_interval=0.02, use_watchdog=False)
    tailer.start()
    try:
        os.remove(path)
        time.sleep(0.15)  # a few poll cycles with the file missing; must not crash
        assert tailer.running

        with open(path, "wb") as f:
            f.write(b"back\n")
        wait_for(lambda: collector.lines_for(str(path)) == ["back"])
    finally:
        tailer.stop()


# -- partial lines ------------------------------------------------------------


def test_partial_line_buffered_until_newline(tmp_path: Path) -> None:
    path = tmp_path / "a.log"
    path.write_text("")

    collector = Collector()
    tailer = MultiTailer([str(path)], collector, poll_interval=0.02, use_watchdog=False)
    tailer.start()
    try:
        with open(path, "ab") as f:
            f.write(b"abc")
        time.sleep(0.1)
        assert collector.all_lines() == []

        with open(path, "ab") as f:
            f.write(b"def\n")
        wait_for(lambda: collector.lines_for(str(path)) == ["abcdef"])
    finally:
        tailer.stop()


# -- multiple files -----------------------------------------------------------


def test_two_files_tagged_with_correct_source(tmp_path: Path) -> None:
    path_a = tmp_path / "a.log"
    path_b = tmp_path / "b.log"
    path_a.write_text("")
    path_b.write_text("")

    collector = Collector()
    tailer = MultiTailer(
        [str(path_a), str(path_b)], collector, poll_interval=0.02, use_watchdog=False
    )
    tailer.start()
    try:
        with open(path_a, "a") as f:
            f.write("a1\n")
        with open(path_b, "a") as f:
            f.write("b1\n")
        wait_for(
            lambda: (
                collector.lines_for(str(path_a)) == ["a1"]
                and collector.lines_for(str(path_b)) == ["b1"]
            )
        )

        with open(path_a, "a") as f:
            f.write("a2\n")
        wait_for(lambda: collector.lines_for(str(path_a)) == ["a1", "a2"])
        assert collector.lines_for(str(path_b)) == ["b1"]
    finally:
        tailer.stop()


# -- encoding -----------------------------------------------------------------


def test_invalid_utf8_is_replaced_not_raised(tmp_path: Path) -> None:
    path = tmp_path / "a.log"
    path.write_bytes(b"")

    collector = Collector()
    tailer = MultiTailer([str(path)], collector, poll_interval=0.02, use_watchdog=False)
    tailer.start()
    try:
        with open(path, "ab") as f:
            f.write(b"bad-\xff\xfe-bytes\n")
        wait_for(lambda: len(collector.lines_for(str(path))) == 1)
        line = collector.lines_for(str(path))[0]
        assert line.startswith("bad-") and line.endswith("-bytes")
        assert "�" in line
    finally:
        tailer.stop()


# -- robustness -----------------------------------------------------------------


def test_on_lines_exception_does_not_kill_worker(tmp_path: Path) -> None:
    path = tmp_path / "a.log"
    path.write_text("")

    calls: list[tuple[str, list[str]]] = []
    lock = threading.Lock()

    def flaky(source: str, lines: list[str]) -> None:
        with lock:
            calls.append((source, list(lines)))
            count = len(calls)
        if count == 1:
            raise RuntimeError("boom")

    tailer = MultiTailer([str(path)], flaky, poll_interval=0.02, use_watchdog=False)
    tailer.start()
    try:
        with open(path, "a") as f:
            f.write("first\n")
        wait_for(lambda: len(calls) >= 1)

        with open(path, "a") as f:
            f.write("second\n")
        wait_for(lambda: len(calls) >= 2)
        assert tailer.running
    finally:
        tailer.stop()


def test_stop_joins_all_threads_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "a.log"
    path.write_text("")
    collector = Collector()

    baseline = {t.ident for t in threading.enumerate()}

    tailer = MultiTailer([str(path)], collector, poll_interval=0.02, use_watchdog=True)
    tailer.start()
    with open(path, "a") as f:
        f.write("x\n")
    wait_for(lambda: collector.lines_for(str(path)) == ["x"])

    spawned = [t for t in threading.enumerate() if t.ident not in baseline]
    assert spawned

    tailer.stop()
    tailer.stop()  # must be safe to call twice

    assert not tailer.running
    wait_for(lambda: all(not t.is_alive() for t in spawned))


# -- watchdog integration ------------------------------------------------------


def test_watchdog_enabled_delivers_append(tmp_path: Path) -> None:
    path = tmp_path / "a.log"
    path.write_text("")

    collector = Collector()
    tailer = MultiTailer([str(path)], collector, poll_interval=0.5, use_watchdog=True)
    tailer.start()
    try:
        with open(path, "a") as f:
            f.write("watched\n")
        wait_for(lambda: collector.lines_for(str(path)) == ["watched"], timeout=5.0)
    finally:
        tailer.stop()


# -- read_tail ------------------------------------------------------------------


def test_read_tail_last_n_lines(tmp_path: Path) -> None:
    path = tmp_path / "a.log"
    path.write_text("".join(f"line{i}\n" for i in range(10)))
    assert read_tail(path, max_lines=3) == ["line7", "line8", "line9"]


def test_read_tail_file_shorter_than_n(tmp_path: Path) -> None:
    path = tmp_path / "a.log"
    path.write_text("only\ntwo\n")
    assert read_tail(path, max_lines=50) == ["only", "two"]


def test_read_tail_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.log"
    path.write_text("")
    assert read_tail(path) == []


def test_read_tail_missing_file(tmp_path: Path) -> None:
    assert read_tail(tmp_path / "does-not-exist.log") == []


def test_read_tail_respects_max_bytes_window(tmp_path: Path) -> None:
    path = tmp_path / "a.log"
    lines = [f"{i:04d}" for i in range(20)]
    path.write_text("\n".join(lines) + "\n")
    result = read_tail(path, max_lines=1000, max_bytes=12)
    assert result == lines[-2:]
