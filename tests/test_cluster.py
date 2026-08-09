"""Tests for error record assembly and error clustering."""

from __future__ import annotations

import os
import random
import subprocess
import sys
import time
from collections.abc import Iterable, Sequence

from loglens.buffer import LineBuffer
from loglens.cluster import Cluster, ErrorRecord, cluster_errors, collect_error_records
from loglens.parsers import Level, LogLine

# -- helpers ------------------------------------------------------------------


def make_line(
    raw: str,
    *,
    source: str = "app.log",
    level: Level | None = None,
    continuation: bool = False,
    message: str | None = None,
) -> LogLine:
    return LogLine(
        source=source,
        raw=raw,
        message=raw if message is None else message,
        level=level,
        continuation=continuation,
    )


def entries(
    *specs: tuple[LogLine, Level | None],
) -> list[tuple[int, LogLine, Level | None]]:
    """Number a list of (line, effective level) pairs from seq 1 upwards."""
    return [(index, line, level) for index, (line, level) in enumerate(specs, start=1)]


def error_head(text: str, *, source: str = "app.log") -> tuple[LogLine, Level | None]:
    return make_line(text, source=source, level=Level.ERROR), Level.ERROR


def info_head(text: str, *, source: str = "app.log") -> tuple[LogLine, Level | None]:
    return make_line(text, source=source, level=Level.INFO), Level.INFO


def cont(text: str, *, source: str = "app.log", level: Level | None = Level.ERROR):
    return make_line(text, source=source, continuation=True), level


def traceback_text(
    frames: Sequence[tuple[str, str]],
    lines: Sequence[int],
    exc: str,
    msg: str,
) -> list[str]:
    out = ["Traceback (most recent call last):"]
    for (path, func), number in zip(frames, lines, strict=True):
        out.append(f'  File "{path}", line {number}, in {func}')
        out.append(f"    result = {func}(item)")
    out.append(f"{exc}: {msg}")
    return out


DEFAULT_FRAMES: tuple[tuple[str, str], ...] = (
    ("app/core/handler.py", "process_request"),
    ("app/db/pool.py", "acquire"),
)


def traced_record(
    seq: int,
    *,
    head: str = "unhandled exception in request handler",
    frames: Sequence[tuple[str, str]] = DEFAULT_FRAMES,
    lines: Sequence[int] | None = None,
    exc: str = "ValueError",
    msg: str = "invalid item state",
    source: str = "app.log",
) -> ErrorRecord:
    numbers = list(lines) if lines is not None else [10 * (i + 1) for i in range(len(frames))]
    trace = [
        make_line(text, source=source, continuation=True)
        for text in traceback_text(frames, numbers, exc, msg)
    ]
    return ErrorRecord(
        seq=seq,
        head=make_line(head, source=source, level=Level.ERROR),
        trace=trace,
    )


def message_record(seq: int, text: str, *, source: str = "app.log") -> ErrorRecord:
    return ErrorRecord(seq=seq, head=make_line(text, source=source, level=Level.ERROR), trace=[])


def signatures(clusters: Iterable[Cluster]) -> list[str]:
    return [cluster.signature for cluster in clusters]


# -- collect_error_records ----------------------------------------------------


def test_collect_empty_input_returns_empty_list() -> None:
    assert collect_error_records([]) == []


def test_collect_single_error_without_continuations() -> None:
    records = collect_error_records(entries(error_head("boom")))
    assert len(records) == 1
    assert records[0].seq == 1
    assert records[0].head.raw == "boom"
    assert records[0].trace == []


def test_collect_attaches_continuations_in_order() -> None:
    records = collect_error_records(
        entries(
            error_head("boom"),
            cont("Traceback (most recent call last):"),
            cont('  File "a.py", line 1, in f'),
            cont("ValueError: bad"),
        )
    )
    assert len(records) == 1
    assert [line.raw for line in records[0].trace] == [
        "Traceback (most recent call last):",
        '  File "a.py", line 1, in f',
        "ValueError: bad",
    ]


def test_collect_keeps_interleaved_sources_apart() -> None:
    records = collect_error_records(
        entries(
            error_head("boom a", source="a.log"),
            error_head("boom b", source="b.log"),
            cont("trace a1", source="a.log"),
            cont("trace b1", source="b.log"),
            cont("trace a2", source="a.log"),
        )
    )
    assert len(records) == 2
    first, second = records
    assert first.head.source == "a.log"
    assert [line.raw for line in first.trace] == ["trace a1", "trace a2"]
    assert second.head.source == "b.log"
    assert [line.raw for line in second.trace] == ["trace b1"]


def test_collect_other_source_real_line_does_not_close_record() -> None:
    records = collect_error_records(
        entries(
            error_head("boom a", source="a.log"),
            info_head("unrelated", source="b.log"),
            cont("trace a1", source="a.log"),
        )
    )
    assert len(records) == 1
    assert [line.raw for line in records[0].trace] == ["trace a1"]


def test_collect_same_source_real_line_closes_record() -> None:
    records = collect_error_records(
        entries(
            error_head("boom"),
            cont("attached"),
            info_head("next real line"),
            cont("orphan now"),
        )
    )
    assert len(records) == 1
    assert [line.raw for line in records[0].trace] == ["attached"]


def test_collect_skips_orphan_continuations() -> None:
    records = collect_error_records(
        entries(
            cont("leading orphan"),
            info_head("hello"),
            cont("orphan after info", level=Level.INFO),
        )
    )
    assert records == []


def test_collect_info_line_never_opens_a_record() -> None:
    records = collect_error_records(
        entries(
            info_head("all good"),
            cont("still fine", level=Level.INFO),
            (make_line("warned", level=Level.WARN), Level.WARN),
            (make_line("unknown level"), None),
        )
    )
    assert records == []


def test_collect_uses_effective_level_not_line_level() -> None:
    # A continuation carries no level of its own; the effective level decides.
    line = make_line("carries no level", continuation=True)
    records = collect_error_records(entries(error_head("boom"), (line, Level.ERROR)))
    assert records[0].trace == [line]


def test_collect_ignores_continuation_with_non_error_effective_level() -> None:
    records = collect_error_records(entries(error_head("boom"), cont("demoted", level=Level.WARN)))
    assert records[0].trace == []


def test_collect_returns_records_in_seq_order() -> None:
    records = collect_error_records(
        entries(
            error_head("first", source="a.log"),
            error_head("second", source="b.log"),
            error_head("third", source="a.log"),
        )
    )
    assert [record.seq for record in records] == [1, 2, 3]


def test_collect_consumes_a_generator_once() -> None:
    stream = iter(entries(error_head("boom"), cont("tail")))
    records = collect_error_records(stream)
    assert len(records) == 1
    assert list(stream) == []


def test_collect_matches_line_buffer_semantics() -> None:
    buffer = LineBuffer(capacity=50)
    buffer.append(make_line("info", level=Level.INFO))
    buffer.append(make_line("boom", level=Level.ERROR))
    buffer.append(make_line('  File "a.py", line 1, in f', continuation=True))
    buffer.append(make_line("other file info", source="b.log", level=Level.INFO))
    buffer.append(make_line("ValueError: bad", continuation=True))
    buffer.append(make_line("recovered", level=Level.INFO))
    buffer.append(make_line("late orphan", continuation=True))

    records = collect_error_records(buffer.since(0))
    assert len(records) == 1
    assert records[0].head.raw == "boom"
    assert [line.raw for line in records[0].trace] == [
        '  File "a.py", line 1, in f',
        "ValueError: bad",
    ]


# -- clustering: traced records -----------------------------------------------


def test_cluster_empty_input_returns_empty_list() -> None:
    assert cluster_errors([]) == []


def test_same_trace_with_volatile_parts_forms_one_cluster() -> None:
    records = [
        traced_record(10, lines=[271, 88], msg="invalid item state", source="a.log"),
        traced_record(20, lines=[12, 400], msg="invalid item state", source="b.log"),
        traced_record(30, lines=[99, 1], msg="invalid item state", source="a.log"),
    ]
    clusters = cluster_errors(records)
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster.count == 3
    assert cluster.first_seq == 10
    assert cluster.last_seq == 30
    assert cluster.sources == frozenset({"a.log", "b.log"})
    assert cluster.example.seq == 30


def test_volatile_exception_messages_do_not_split_a_cluster() -> None:
    records = [
        traced_record(1, msg="user 4211 not found at 0x7f8b2c3d"),
        traced_record(2, msg="user 9 not found at 0x1a2b3c4d"),
        traced_record(3, msg="user 100000 not found at 0xdeadbeef"),
    ]
    clusters = cluster_errors(records)
    assert len(clusters) == 1
    assert clusters[0].count == 3


def test_different_exception_type_same_frames_stays_separate() -> None:
    clusters = cluster_errors(
        [
            traced_record(1, exc="ValueError"),
            traced_record(2, exc="KeyError"),
        ]
    )
    assert len(clusters) == 2
    assert {cluster.count for cluster in clusters} == {1}


def test_same_exception_different_frame_chain_stays_separate() -> None:
    other_frames = (("app/api/routes.py", "dispatch"), ("worker/tasks.py", "run_task"))
    clusters = cluster_errors(
        [
            traced_record(1),
            traced_record(2, frames=other_frames),
        ]
    )
    assert len(clusters) == 2


def test_same_files_different_functions_stay_separate() -> None:
    swapped = (("app/core/handler.py", "process_request"), ("app/db/pool.py", "release"))
    clusters = cluster_errors([traced_record(1), traced_record(2, frames=swapped)])
    assert len(clusters) == 2


def test_head_message_does_not_split_traced_records() -> None:
    clusters = cluster_errors(
        [
            traced_record(1, head="unhandled exception in request handler"),
            traced_record(2, head="request failed"),
        ]
    )
    assert len(clusters) == 1
    assert clusters[0].count == 2


def framed_record(seq: int, head: str, line_no: int, *, func: str = "acquire") -> ErrorRecord:
    """A record whose frames survived but whose summary line did not.

    Some parsers only treat indented lines as continuations, so the trailing
    `SomeError: ...` line lands outside the record.
    """
    trace = [
        make_line("Traceback (most recent call last):", continuation=True),
        make_line(f'  File "app/db/pool.py", line {line_no}, in {func}', continuation=True),
        make_line("    conn = pool.get()", continuation=True),
    ]
    return ErrorRecord(seq=seq, head=make_line(head, level=Level.ERROR), trace=trace)


def test_frames_without_summary_line_group_by_head_message() -> None:
    clusters = cluster_errors(
        [
            framed_record(1, "app.db: failed to connect to shard 3", 88),
            framed_record(2, "app.db: failed to connect to shard 17", 412),
        ]
    )
    assert len(clusters) == 1
    assert clusters[0].count == 2
    assert clusters[0].title == "app.db: failed to connect to shard <n>"


def test_frames_without_summary_line_keep_distinct_messages_apart() -> None:
    clusters = cluster_errors(
        [
            framed_record(1, "app.db: failed to connect", 88),
            framed_record(2, "app.db: permission denied", 88),
        ]
    )
    assert len(clusters) == 2
    assert {cluster.title for cluster in clusters} == {
        "app.db: failed to connect",
        "app.db: permission denied",
    }


def test_summary_without_frames_still_uses_the_exception_type() -> None:
    def summarized(seq: int, head: str, detail: str) -> ErrorRecord:
        return ErrorRecord(
            seq=seq,
            head=make_line(head, level=Level.ERROR),
            trace=[make_line(f"OSError: {detail}", continuation=True)],
        )

    clusters = cluster_errors(
        [
            summarized(1, "write failed", "disk quota exceeded for 4211 blocks"),
            summarized(2, "write failed", "disk quota exceeded for 9 blocks"),
            summarized(3, "read failed", "disk quota exceeded for 7 blocks"),
        ]
    )
    assert len(clusters) == 2
    assert clusters[0].count == 2
    assert clusters[0].title == "OSError: disk quota exceeded for <n> blocks"


def test_exception_shaped_head_without_a_trace_is_message_only() -> None:
    clusters = cluster_errors(
        [
            message_record(1, "ValueError: bad id 12"),
            message_record(2, "ValueError: bad id 99"),
        ]
    )
    assert len(clusters) == 1
    assert clusters[0].title == "ValueError: bad id <n>"


def test_absolute_and_relative_frame_paths_group_together() -> None:
    relative = (("app/core/handler.py", "process_request"), ("app/db/pool.py", "acquire"))
    absolute = (
        ("/srv/deploy/build-7712/app/core/handler.py", "process_request"),
        ("/srv/deploy/build-7712/app/db/pool.py", "acquire"),
    )
    clusters = cluster_errors(
        [traced_record(1, frames=relative), traced_record(2, frames=absolute)]
    )
    assert len(clusters) == 1


def test_traceback_embedded_in_head_message_is_used() -> None:
    # JSON logs keep the whole traceback inside one physical line.
    body = "\n".join(
        ["boom", *traceback_text(DEFAULT_FRAMES, [3, 4], "ValueError", "invalid item state")]
    )
    embedded = ErrorRecord(seq=5, head=make_line(body, level=Level.ERROR), trace=[])
    clusters = cluster_errors([embedded, traced_record(6, lines=[80, 90])])
    assert len(clusters) == 1
    assert clusters[0].count == 2


def test_title_for_traced_record_is_type_and_masked_message() -> None:
    clusters = cluster_errors([traced_record(1, exc="TimeoutError", msg="timed out after 30s")])
    assert clusters[0].title == "TimeoutError: timed out after <n>s"


def test_titles_are_single_line_and_bounded() -> None:
    long_message = "failure " + " ".join(f"detail{index}" for index in range(80))
    clusters = cluster_errors(
        [
            traced_record(1, msg=long_message),
            message_record(2, "plain " + long_message),
        ]
    )
    for cluster in clusters:
        assert "\n" not in cluster.title
        assert len(cluster.title) <= 120


def test_deep_recursive_frames_collapse_into_one_cluster() -> None:
    deep = (("app/core/walk.py", "walk"),) * 200
    clusters = cluster_errors(
        [
            traced_record(1, frames=deep, lines=list(range(200)), exc="RecursionError", msg="deep"),
            traced_record(
                2, frames=deep, lines=list(range(500, 700)), exc="RecursionError", msg="deep"
            ),
        ]
    )
    assert len(clusters) == 1
    assert len(clusters[0].signature) < 2000


# -- clustering: message-only records -----------------------------------------


def test_numbers_in_messages_are_masked() -> None:
    clusters = cluster_errors(
        [
            message_record(1, "timeout waiting for upstream after 30ms"),
            message_record(2, "timeout waiting for upstream after 4001ms"),
            message_record(3, "timeout waiting for upstream after 7.25ms"),
        ]
    )
    assert len(clusters) == 1
    assert clusters[0].count == 3
    assert clusters[0].title == "timeout waiting for upstream after <n>ms"


def test_uuids_in_messages_are_masked() -> None:
    clusters = cluster_errors(
        [
            message_record(1, "job 3f2504e0-4f89-11d3-9a0c-0305e82c3301 aborted"),
            message_record(2, "job 7c9e6679-7425-40de-944b-e07fc1f90ae7 aborted"),
        ]
    )
    assert len(clusters) == 1
    assert clusters[0].title == "job <uuid> aborted"


def test_hex_addresses_in_messages_are_masked() -> None:
    clusters = cluster_errors(
        [
            message_record(1, "cannot free buffer at 0x7f8b2c3d4e5f"),
            message_record(2, "cannot free buffer at 0x55d0a1b2c3d4"),
        ]
    )
    assert len(clusters) == 1
    assert clusters[0].title == "cannot free buffer at <hex>"


def test_quoted_payloads_in_messages_are_masked() -> None:
    clusters = cluster_errors(
        [
            message_record(1, 'rejected payload "order-1187" from queue'),
            message_record(2, 'rejected payload "order-99" from queue'),
            message_record(3, "rejected payload 'order-4' from queue"),
        ]
    )
    assert len(clusters) == 1
    assert clusters[0].count == 3


def test_embedded_timestamps_in_messages_are_masked() -> None:
    clusters = cluster_errors(
        [
            message_record(1, "lease expired at 2026-08-10T02:11:33.123Z for worker"),
            message_record(2, "lease expired at 2026-08-11T23:59:00.000Z for worker"),
        ]
    )
    assert len(clusters) == 1
    assert clusters[0].title == "lease expired at <ts> for worker"


def test_mixed_id_tokens_do_not_split_a_cluster() -> None:
    clusters = cluster_errors(
        [
            message_record(1, "request req-4f2a for user user1234 failed"),
            message_record(2, "request req-9b31 for user user9 failed"),
        ]
    )
    assert len(clusters) == 1


def test_different_message_templates_do_not_merge() -> None:
    clusters = cluster_errors(
        [
            message_record(1, "failed to connect to database"),
            message_record(2, "failed to connect to cache"),
            message_record(3, "timeout waiting for upstream response"),
            message_record(4, "invalid payload received"),
        ]
    )
    assert len(clusters) == 4
    assert all(cluster.count == 1 for cluster in clusters)


def test_case_and_spacing_differences_group_together() -> None:
    clusters = cluster_errors(
        [
            message_record(1, "Connection reset by peer"),
            message_record(2, "connection   reset by peer"),
        ]
    )
    assert len(clusters) == 1


def test_leading_timestamp_and_level_prefix_are_ignored() -> None:
    clusters = cluster_errors(
        [
            message_record(1, "2026-08-10 02:11:33,123 ERROR connection reset by peer"),
            message_record(2, "2026-08-11 09:00:00,001 CRITICAL connection reset by peer"),
        ]
    )
    assert len(clusters) == 1
    assert clusters[0].title == "connection reset by peer"


def test_message_only_and_traced_records_do_not_mix() -> None:
    clusters = cluster_errors([traced_record(1), message_record(2, "invalid item state")])
    assert len(clusters) == 2


def test_near_identical_long_messages_merge_via_similarity_pass() -> None:
    base = "worker pool exhausted while scheduling background export job for tenant 42"
    extended = base + " again"
    clusters = cluster_errors(
        [
            message_record(1, base),
            message_record(2, base),
            message_record(3, extended),
        ]
    )
    assert len(clusters) == 1
    assert clusters[0].count == 3
    assert clusters[0].last_seq == 3


def test_short_similar_messages_are_not_merged() -> None:
    clusters = cluster_errors(
        [
            message_record(1, "disk full on volume data"),
            message_record(2, "disk full on volume logs"),
        ]
    )
    assert len(clusters) == 2


def test_long_messages_differing_by_a_word_are_not_merged() -> None:
    clusters = cluster_errors(
        [
            message_record(1, "worker pool exhausted while scheduling export job for tenant 42"),
            message_record(2, "worker pool exhausted while cancelling export job for tenant 42"),
        ]
    )
    assert len(clusters) == 2


# -- ordering, robustness, determinism ----------------------------------------


def test_clusters_are_sorted_by_count_descending() -> None:
    records = [message_record(index, "alpha failure") for index in range(1, 4)]
    records.append(message_record(10, "beta failure"))
    records.extend(message_record(index, "gamma failure") for index in (20, 21))
    clusters = cluster_errors(records)
    assert [cluster.count for cluster in clusters] == [3, 2, 1]
    assert clusters[0].title == "alpha failure"


def test_count_ties_are_broken_by_newest_seq() -> None:
    clusters = cluster_errors(
        [
            message_record(1, "older failure"),
            message_record(2, "older failure"),
            message_record(3, "newer failure"),
            message_record(4, "newer failure"),
        ]
    )
    assert [cluster.title for cluster in clusters] == ["newer failure", "older failure"]
    assert [cluster.last_seq for cluster in clusters] == [4, 2]


def test_example_is_the_most_recent_record_even_when_seqs_arrive_unordered() -> None:
    newest = message_record(90, "flush failed")
    records = [message_record(10, "flush failed"), newest, message_record(50, "flush failed")]
    cluster = cluster_errors(records)[0]
    assert cluster.example is newest
    assert (cluster.first_seq, cluster.last_seq) == (10, 90)


def test_java_style_continuations_group_by_frames() -> None:
    def java_record(seq: int, line_no: int, request: str) -> ErrorRecord:
        trace = [
            make_line(text, continuation=True)
            for text in (
                f"\tat com.foo.svc.Handler.handle(Handler.java:{line_no})",
                "\tat com.foo.svc.Router.route(Router.java:12)",
                "\t... 23 more",
                f"Caused by: java.lang.IllegalStateException: bad request req-{request}",
            )
        ]
        return ErrorRecord(seq=seq, head=make_line("boom", level=Level.ERROR), trace=trace)

    clusters = cluster_errors([java_record(1, 42, "8813"), java_record(2, 118, "4f2a")])
    assert len(clusters) == 1
    assert clusters[0].count == 2
    # Frame line numbers and the id in the cause message are both volatile.
    assert clusters[0].title == "java.lang.IllegalStateException: bad request req-<hex>"


def test_java_records_with_different_frames_stay_separate() -> None:
    def java_record(seq: int, cls: str) -> ErrorRecord:
        trace = [
            make_line(f"\tat com.foo.svc.{cls}.handle({cls}.java:42)", continuation=True),
            make_line("Caused by: java.lang.IllegalStateException: bad", continuation=True),
        ]
        return ErrorRecord(seq=seq, head=make_line("boom", level=Level.ERROR), trace=trace)

    clusters = cluster_errors([java_record(1, "Handler"), java_record(2, "Router")])
    assert len(clusters) == 2


def test_junk_input_never_raises_and_degrades_gracefully() -> None:
    junk = [
        message_record(1, ""),
        message_record(2, "   \t  "),
        message_record(3, "\x1b[31m\x00\x07garbled\x1b[0m"),
        message_record(4, "☃☃☃ ünïcödé ﷽ junk"),
        message_record(5, "'unterminated quote"),
        ErrorRecord(seq=6, head=make_line("no trace at all", level=Level.ERROR), trace=[]),
        ErrorRecord(
            seq=7,
            head=make_line("weird", level=Level.ERROR),
            trace=[make_line("}{)(*&^%$#@!", continuation=True)],
        ),
    ]
    clusters = cluster_errors(junk)
    assert sum(cluster.count for cluster in clusters) == len(junk)
    for cluster in clusters:
        assert cluster.title
        assert "\n" not in cluster.title


def test_structurally_broken_records_never_raise() -> None:
    good = message_record(1, "real failure")
    broken_trace = message_record(2, "half broken")
    broken_trace.trace = None  # a caller handed us something that is not a list
    broken_seq = message_record(3, "no usable seq")
    broken_seq.seq = None

    clusters = cluster_errors([good, broken_trace, broken_seq])
    # The unusable record is dropped; the salvageable one becomes its own
    # cluster rather than taking the caller down.
    assert sum(cluster.count for cluster in clusters) == 2
    assert any(cluster.title == "(unparsed error record)" for cluster in clusters)


def test_repeated_runs_produce_identical_output() -> None:
    records = [
        traced_record(1, lines=[3, 4]),
        traced_record(2, lines=[5, 6], exc="KeyError"),
        message_record(3, "disk full on volume data"),
        message_record(4, "disk full on volume data"),
    ]
    first = cluster_errors(records)
    second = cluster_errors(records)
    assert signatures(first) == signatures(second)
    assert [cluster.count for cluster in first] == [cluster.count for cluster in second]


def test_output_does_not_depend_on_hash_seed() -> None:
    script = """
import json
from loglens.cluster import ErrorRecord, cluster_errors
from loglens.parsers import Level, LogLine

def line(raw, cont=False):
    return LogLine(source="a.log", raw=raw, message=raw, continuation=cont,
                   level=None if cont else Level.ERROR)

records = []
for index in range(1, 60):
    kind = index % 3
    if kind == 0:
        trace = [line('  File "app/db/pool.py", line %d, in acquire' % index, True),
                 line("ValueError: bad id %d" % index, True)]
        records.append(ErrorRecord(seq=index, head=line("boom"), trace=trace))
    elif kind == 1:
        records.append(ErrorRecord(seq=index, head=line("timeout after %dms" % index), trace=[]))
    else:
        records.append(ErrorRecord(seq=index, head=line("queue depth %d too high" % index),
                                   trace=[]))

print(json.dumps([[c.signature, c.title, c.count, c.first_seq, c.last_seq,
                   sorted(c.sources), c.example.seq] for c in cluster_errors(records)]))
"""
    outputs = []
    for seed in ("0", "1", "424242"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        outputs.append(result.stdout)
    assert outputs[0] == outputs[1] == outputs[2]
    assert outputs[0].strip()


# -- generative stress --------------------------------------------------------

_TRACE_TEMPLATES: tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...] = (
    ("ValueError", "invalid item state", (("app/core/handler.py", "process_request"),)),
    ("KeyError", "'user_id'", (("app/core/handler.py", "process_request"),)),
    ("TimeoutError", "connection timed out", (("app/db/pool.py", "acquire"),)),
    (
        "ConnectionError",
        "connection refused",
        (("app/db/pool.py", "acquire"), ("app/db/driver.py", "connect")),
    ),
    (
        "RuntimeError",
        "unexpected null in response",
        (("app/api/routes.py", "dispatch"), ("app/api/client.py", "fetch")),
    ),
    ("OSError", "disk quota exceeded", (("worker/tasks.py", "run_task"),)),
    (
        "ZeroDivisionError",
        "division by zero",
        (("worker/tasks.py", "run_task"), ("worker/math.py", "ratio")),
    ),
    ("AttributeError", "'NoneType' has no attribute", (("app/api/routes.py", "dispatch"),)),
    ("PermissionError", "permission denied", (("app/fs/store.py", "write"),)),
    ("MemoryError", "out of memory", (("app/cache/lru.py", "admit"),)),
)

_MESSAGE_TEMPLATES: tuple[str, ...] = (
    "failed to connect to database shard {n}",
    "timeout waiting for upstream response after {n}ms",
    "invalid payload received from client {uuid}",
    "permission denied writing to /var/log/app/out{n}.log",
    "connection reset by peer at 0x{hex}",
    "cache eviction storm detected, dropped {n} keys",
    "session {uuid} expired before commit",
    "queue depth {n} exceeds high watermark",
    'rejected message "{word}-{n}" from producer',
    "replication lag {n}s on replica {n}",
)


def build_stress_records(count: int, seed: int = 20250810) -> list[ErrorRecord]:
    rng = random.Random(seed)
    records: list[ErrorRecord] = []
    templates = len(_TRACE_TEMPLATES) + len(_MESSAGE_TEMPLATES)
    for index in range(count):
        seq = index + 1
        choice = index % templates
        source = f"app{seq % 3}.log"
        if choice < len(_TRACE_TEMPLATES):
            exc, msg, frames = _TRACE_TEMPLATES[choice]
            numbers = [rng.randint(1, 900) for _ in frames]
            records.append(
                traced_record(
                    seq,
                    head=f"request {rng.randint(1, 10**6)} failed",
                    frames=frames,
                    lines=numbers,
                    exc=exc,
                    msg=f"{msg} (id {rng.randint(1, 10**7)})",
                    source=source,
                )
            )
        else:
            template = _MESSAGE_TEMPLATES[choice - len(_TRACE_TEMPLATES)]
            text = template.format(
                n=rng.randint(1, 10**6),
                uuid=f"{rng.getrandbits(32):08x}-1234-5678-9abc-{rng.getrandbits(48):012x}",
                hex=f"{rng.getrandbits(48):012x}",
                word=rng.choice(("order", "invoice", "shipment")),
            )
            records.append(message_record(seq, text, source=source))
    return records


def test_generative_stress_recovers_the_template_count() -> None:
    records = build_stress_records(2000)
    started = time.perf_counter()
    clusters = cluster_errors(records)
    elapsed = time.perf_counter() - started

    expected_templates = len(_TRACE_TEMPLATES) + len(_MESSAGE_TEMPLATES)
    assert len(clusters) == expected_templates
    assert sum(cluster.count for cluster in clusters) == len(records)
    # 2000 records spread evenly over 20 templates: 100 apiece.
    assert {cluster.count for cluster in clusters} == {100}
    assert elapsed < 2.0


def test_generative_stress_keeps_bookkeeping_consistent() -> None:
    records = build_stress_records(600)
    by_seq = {record.seq: record for record in records}
    clusters = cluster_errors(records)

    seen_last: set[int] = set()
    for cluster in clusters:
        assert cluster.first_seq <= cluster.last_seq
        assert cluster.example.seq == cluster.last_seq
        assert by_seq[cluster.last_seq] is cluster.example
        assert cluster.sources <= {"app0.log", "app1.log", "app2.log"}
        assert cluster.last_seq not in seen_last
        seen_last.add(cluster.last_seq)


def test_collect_then_cluster_end_to_end() -> None:
    buffer = LineBuffer(capacity=500)
    for round_index in range(5):
        for source in ("a.log", "b.log"):
            buffer.append(make_line("healthy", source=source, level=Level.INFO))
            buffer.append(
                make_line(f"request {round_index} failed", source=source, level=Level.ERROR)
            )
            for text in traceback_text(DEFAULT_FRAMES, [round_index, 40], "ValueError", "bad"):
                buffer.append(make_line(text, source=source, continuation=True))

    clusters = cluster_errors(collect_error_records(buffer.since(0)))
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster.count == 10
    assert cluster.sources == frozenset({"a.log", "b.log"})
    assert cluster.title == "ValueError: bad"
