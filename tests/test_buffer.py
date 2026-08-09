"""Tests for the ring buffer and its search/index helpers."""

from __future__ import annotations

import re
import threading
import time

from loglens.buffer import LineBuffer
from loglens.parsers import Level, LogLine


def make_line(
    *,
    source: str = "app.log",
    raw: str = "line",
    message: str | None = None,
    level: Level | None = None,
    continuation: bool = False,
    arrival: float = 0.0,
) -> LogLine:
    return LogLine(
        source=source,
        raw=raw,
        message=message if message is not None else raw,
        level=level,
        continuation=continuation,
        arrival=arrival,
    )


# -- seq assignment / total_appended -----------------------------------------


def test_seq_assigned_from_one_and_monotonic() -> None:
    buf = LineBuffer(capacity=10)
    first = buf.append(make_line(raw="a"))
    second = buf.append(make_line(raw="b"))
    third = buf.append(make_line(raw="c"))
    assert (first, second, third) == (1, 2, 3)
    assert buf.total_appended == 3


def test_total_appended_includes_evicted() -> None:
    buf = LineBuffer(capacity=3)
    for i in range(10):
        buf.append(make_line(raw=str(i)))
    assert buf.total_appended == 10
    assert len(buf) == 3


def test_extend_assigns_sequential_seqs() -> None:
    buf = LineBuffer(capacity=10)
    buf.extend(make_line(raw=str(i)) for i in range(4))
    assert buf.total_appended == 4
    assert [line.raw for _seq, line in buf.snapshot()] == ["0", "1", "2", "3"]


# -- capacity eviction --------------------------------------------------------


def test_capacity_eviction_drops_oldest() -> None:
    buf = LineBuffer(capacity=5)
    seqs = [buf.append(make_line(raw=str(i))) for i in range(8)]
    assert len(buf) == 5
    for seq in seqs[:3]:
        assert buf.get(seq) is None
    for seq in seqs[3:]:
        assert buf.get(seq) is not None


def test_get_live_seq_returns_correct_line() -> None:
    buf = LineBuffer(capacity=5)
    buf.append(make_line(raw="first"))
    second_seq = buf.append(make_line(raw="second"))
    line = buf.get(second_seq)
    assert line is not None
    assert line.raw == "second"


def test_get_unknown_future_seq_returns_none() -> None:
    buf = LineBuffer(capacity=5)
    buf.append(make_line(raw="only"))
    assert buf.get(999) is None


def test_get_empty_buffer_returns_none() -> None:
    buf = LineBuffer(capacity=5)
    assert buf.get(1) is None


def test_snapshot_ordering_oldest_first() -> None:
    buf = LineBuffer(capacity=5)
    for i in range(5):
        buf.append(make_line(raw=str(i)))
    snap = buf.snapshot()
    assert [line.raw for _seq, line in snap] == ["0", "1", "2", "3", "4"]
    assert [seq for seq, _line in snap] == [1, 2, 3, 4, 5]


# -- arrival stamping -----------------------------------------------------------


def test_arrival_stamped_when_zero() -> None:
    buf = LineBuffer(capacity=5)
    before = time.time()
    seq = buf.append(make_line(raw="x", arrival=0.0))
    after = time.time()
    line = buf.get(seq)
    assert line is not None
    assert before <= line.arrival <= after


def test_arrival_preserved_when_preset() -> None:
    buf = LineBuffer(capacity=5)
    seq = buf.append(make_line(raw="x", arrival=12345.5))
    line = buf.get(seq)
    assert line is not None
    assert line.arrival == 12345.5


# -- view: pattern / errors_only ------------------------------------------------


def test_view_pattern_filters_on_raw() -> None:
    buf = LineBuffer(capacity=10)
    buf.append(make_line(raw="hello world"))
    buf.append(make_line(raw="goodbye world"))
    buf.append(make_line(raw="hello again"))
    result = buf.view(pattern=re.compile("hello"))
    assert [line.raw for _seq, line in result] == ["hello world", "hello again"]


def test_view_pattern_is_search_not_match() -> None:
    buf = LineBuffer(capacity=10)
    buf.append(make_line(raw="prefix-needle-suffix"))
    result = buf.view(pattern=re.compile("needle"))
    assert len(result) == 1


def test_view_errors_only_keeps_effective_error_level() -> None:
    buf = LineBuffer(capacity=10)
    buf.append(make_line(raw="info line", level=Level.INFO))
    buf.append(make_line(raw="error line", level=Level.ERROR))
    buf.append(make_line(raw="warn line", level=Level.WARN))
    result = buf.view(errors_only=True)
    assert [line.raw for _seq, line in result] == ["error line"]


def test_view_pattern_and_errors_only_combined() -> None:
    buf = LineBuffer(capacity=10)
    buf.append(make_line(raw="error: disk full", level=Level.ERROR))
    buf.append(make_line(raw="error: retrying, not fatal", level=Level.WARN))
    buf.append(make_line(raw="info: disk ok", level=Level.INFO))
    result = buf.view(pattern=re.compile("disk"), errors_only=True)
    assert [line.raw for _seq, line in result] == ["error: disk full"]


def test_view_no_filters_returns_everything() -> None:
    buf = LineBuffer(capacity=10)
    for i in range(3):
        buf.append(make_line(raw=str(i)))
    result = buf.view()
    assert [line.raw for _seq, line in result] == ["0", "1", "2"]


# -- continuation attachment -----------------------------------------------------


def test_continuation_after_error_included_in_errors_only() -> None:
    buf = LineBuffer(capacity=10)
    buf.append(make_line(source="app", raw="Traceback", level=Level.ERROR))
    buf.append(make_line(source="app", raw="  File x.py", continuation=True))
    buf.append(make_line(source="app", raw="  ValueError", continuation=True))
    result = buf.view(errors_only=True)
    assert [line.raw for _seq, line in result] == [
        "Traceback",
        "  File x.py",
        "  ValueError",
    ]


def test_continuation_after_info_excluded_from_errors_only() -> None:
    buf = LineBuffer(capacity=10)
    buf.append(make_line(source="app", raw="starting up", level=Level.INFO))
    buf.append(make_line(source="app", raw="  details", continuation=True))
    result = buf.view(errors_only=True)
    assert result == []


def test_continuation_interleaved_sources_attach_independently() -> None:
    buf = LineBuffer(capacity=10)
    buf.append(make_line(source="a", raw="a-error", level=Level.ERROR))
    buf.append(make_line(source="b", raw="b-info", level=Level.INFO))
    buf.append(make_line(source="a", raw="a-continuation", continuation=True))
    buf.append(make_line(source="b", raw="b-continuation", continuation=True))
    result = buf.view(errors_only=True)
    assert [line.raw for _seq, line in result] == ["a-error", "a-continuation"]


def test_continuation_uses_most_recent_non_continuation_level() -> None:
    buf = LineBuffer(capacity=10)
    buf.append(make_line(source="app", raw="Traceback", level=Level.ERROR))
    buf.append(make_line(source="app", raw="an info line resets it", level=Level.INFO))
    buf.append(make_line(source="app", raw="  frame", continuation=True))
    result = buf.view(errors_only=True)
    # The original ERROR line still matches on its own merits; the
    # continuation now attaches to the intervening INFO line, not it.
    assert [line.raw for _seq, line in result] == ["Traceback"]


def test_continuation_effective_level_survives_parent_eviction() -> None:
    # Parent (the ERROR line) predates what's still physically in the
    # buffer once capacity forces it out, but the per-source last-level
    # tracking was computed at append time and outlives the eviction.
    buf = LineBuffer(capacity=2)
    buf.append(make_line(source="other", raw="filler", level=Level.INFO))  # seq 1
    traceback_seq = buf.append(make_line(source="app", raw="Traceback", level=Level.ERROR))
    buf.append(make_line(source="other", raw="pad", level=Level.INFO))  # evicts seq 1
    continuation_seq = buf.append(
        make_line(source="app", raw="  frame", continuation=True)
    )  # evicts the Traceback line itself

    assert buf.get(traceback_seq) is None
    assert buf.get(continuation_seq) is not None

    result = buf.view(errors_only=True)
    assert [line.raw for _seq, line in result] == ["  frame"]


def test_continuation_with_no_prior_line_for_source_has_no_effective_level() -> None:
    buf = LineBuffer(capacity=10)
    seq = buf.append(make_line(source="mystery", raw="  orphan frame", continuation=True))
    assert buf.get(seq) is not None
    result = buf.view(errors_only=True)
    assert result == []


# -- view: limit ------------------------------------------------------------------


def test_view_limit_returns_last_n_matches_oldest_first() -> None:
    buf = LineBuffer(capacity=20)
    for i in range(10):
        buf.append(make_line(raw=f"item {i}"))
    result = buf.view(pattern=re.compile("item"), limit=3)
    assert [line.raw for _seq, line in result] == ["item 7", "item 8", "item 9"]


def test_view_limit_larger_than_matches_returns_all() -> None:
    buf = LineBuffer(capacity=20)
    for i in range(3):
        buf.append(make_line(raw=f"item {i}"))
    result = buf.view(pattern=re.compile("item"), limit=100)
    assert [line.raw for _seq, line in result] == ["item 0", "item 1", "item 2"]


def test_view_limit_skips_non_matching_lines() -> None:
    buf = LineBuffer(capacity=20)
    buf.append(make_line(raw="keep 1"))
    buf.append(make_line(raw="skip"))
    buf.append(make_line(raw="keep 2"))
    buf.append(make_line(raw="skip"))
    buf.append(make_line(raw="keep 3"))
    result = buf.view(pattern=re.compile("keep"), limit=2)
    assert [line.raw for _seq, line in result] == ["keep 2", "keep 3"]


def test_view_limit_zero_returns_empty() -> None:
    buf = LineBuffer(capacity=10)
    buf.append(make_line(raw="anything"))
    assert buf.view(limit=0) == []


# -- search -------------------------------------------------------------------


def test_search_returns_ascending_seqs_matching_raw() -> None:
    buf = LineBuffer(capacity=20)
    seqs = [
        buf.append(make_line(raw=f"MATCH {i}" if i % 2 == 0 else f"line {i}")) for i in range(5)
    ]
    result = buf.search(re.compile("MATCH"))
    assert result == [seqs[0], seqs[2], seqs[4]]
    assert result == sorted(result)


def test_search_no_matches_returns_empty_list() -> None:
    buf = LineBuffer(capacity=5)
    buf.append(make_line(raw="nothing interesting"))
    assert buf.search(re.compile("zzz")) == []


# -- error_counts ---------------------------------------------------------------


def test_error_counts_buckets_deterministic() -> None:
    buf = LineBuffer(capacity=50)
    now = 1_000_000.0
    bucket_seconds = 60
    buckets = 5
    # window covers the 300s before `now`; one ERROR line per 60s bucket,
    # oldest offset first so append order matches chronological order.
    offsets = [290, 230, 170, 110, 50]
    for offset in offsets:
        buf.append(make_line(source="app", raw="boom", level=Level.ERROR, arrival=now - offset))
    counts = buf.error_counts(bucket_seconds=bucket_seconds, buckets=buckets, now=now)
    assert counts == [1, 1, 1, 1, 1]


def test_error_counts_empty_window_returns_zeros() -> None:
    buf = LineBuffer(capacity=10)
    counts = buf.error_counts(bucket_seconds=60, buckets=4, now=1000.0)
    assert counts == [0, 0, 0, 0]


def test_error_counts_ignores_lines_outside_window() -> None:
    buf = LineBuffer(capacity=10)
    now = 1_000_000.0
    buf.append(make_line(raw="too old", level=Level.ERROR, arrival=now - 10_000))
    buf.append(make_line(raw="in window", level=Level.ERROR, arrival=now - 30))
    counts = buf.error_counts(bucket_seconds=60, buckets=5, now=now)
    assert sum(counts) == 1
    assert counts[-1] == 1


def test_error_counts_continuation_lines_not_counted() -> None:
    buf = LineBuffer(capacity=10)
    now = 1_000_000.0
    buf.append(make_line(source="app", raw="Traceback", level=Level.ERROR, arrival=now - 20))
    buf.append(make_line(source="app", raw="  frame", continuation=True, arrival=now - 19))
    buf.append(make_line(source="app", raw="  frame2", continuation=True, arrival=now - 18))
    counts = buf.error_counts(bucket_seconds=60, buckets=5, now=now)
    assert sum(counts) == 1


def test_error_counts_non_error_lines_not_counted() -> None:
    buf = LineBuffer(capacity=10)
    now = 1_000_000.0
    buf.append(make_line(raw="info", level=Level.INFO, arrival=now - 10))
    buf.append(make_line(raw="warn", level=Level.WARN, arrival=now - 5))
    counts = buf.error_counts(bucket_seconds=60, buckets=5, now=now)
    assert sum(counts) == 0


def test_error_counts_window_early_exit_does_not_miscount() -> None:
    buf = LineBuffer(capacity=100)
    now = 1_000_000.0
    # A long run of ancient entries appended first, then a handful inside
    # the trailing window. The backward scan should stop as soon as it
    # walks past the window boundary into the ancient run, without
    # dropping any of the in-window entries counted before that point.
    for i in range(50):
        buf.append(make_line(raw="ancient", level=Level.ERROR, arrival=now - 100_000 - i))
    for offset in (250, 200, 150, 100, 50, 1):
        buf.append(make_line(raw="recent", level=Level.ERROR, arrival=now - offset))
    counts = buf.error_counts(bucket_seconds=60, buckets=5, now=now)
    assert sum(counts) == 6


def test_error_counts_bucket_boundary_values() -> None:
    buf = LineBuffer(capacity=10)
    now = 1000.0
    bucket_seconds = 60
    buckets = 3
    window_start = now - bucket_seconds * buckets  # 820.0
    buf.append(make_line(raw="edge-old", level=Level.ERROR, arrival=window_start))
    buf.append(make_line(raw="edge-new", level=Level.ERROR, arrival=now))
    counts = buf.error_counts(bucket_seconds=bucket_seconds, buckets=buckets, now=now)
    assert counts == [1, 0, 1]


def test_error_counts_defaults_now_to_current_time() -> None:
    buf = LineBuffer(capacity=10)
    buf.append(make_line(raw="fresh error", level=Level.ERROR, arrival=time.time()))
    counts = buf.error_counts(bucket_seconds=60, buckets=15)
    assert sum(counts) == 1


# -- thread safety ----------------------------------------------------------------


def test_concurrent_writer_and_reader_no_exceptions() -> None:
    buf = LineBuffer(capacity=200)
    stop = threading.Event()
    errors: list[BaseException] = []

    def writer() -> None:
        try:
            i = 0
            while not stop.is_set():
                level = Level.ERROR if i % 5 == 0 else Level.INFO
                buf.extend([make_line(source="app", raw=f"line {i}", level=level)])
                i += 1
        except BaseException as exc:  # noqa: BLE001 - captured for the assertion below
            errors.append(exc)

    writer_thread = threading.Thread(target=writer)
    writer_thread.start()
    try:
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            buf.view(pattern=re.compile("line"), limit=10)
            buf.search(re.compile("line"))
            buf.error_counts(bucket_seconds=1, buckets=5)
            buf.snapshot()
            len(buf)
    finally:
        stop.set()
        writer_thread.join(timeout=5.0)

    assert not writer_thread.is_alive()
    assert errors == []
    assert len(buf) <= buf.capacity
    assert buf.total_appended >= len(buf)
    for seq, line in buf.snapshot():
        assert buf.get(seq) is line
