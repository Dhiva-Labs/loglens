"""Tests for log format parsers and format auto-detection."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from loglens.parsers import (
    Level,
    detect_format,
    generic,
    jsonlog,
    nginx,
    parser_for_file,
    sample_file,
    syslog,
)

# ---------------------------------------------------------------------------
# Level
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("trace", Level.DEBUG),
        ("TRACE", Level.DEBUG),
        ("debug", Level.DEBUG),
        ("DEBUG", Level.DEBUG),
        ("info", Level.INFO),
        ("notice", Level.INFO),
        ("NOTICE", Level.INFO),
        ("warn", Level.WARN),
        ("warning", Level.WARN),
        ("WARNING", Level.WARN),
        ("error", Level.ERROR),
        ("err", Level.ERROR),
        ("critical", Level.ERROR),
        ("crit", Level.ERROR),
        ("fatal", Level.ERROR),
        ("alert", Level.ERROR),
        ("emerg", Level.ERROR),
        ("  Error  ", Level.ERROR),
    ],
)
def test_level_from_name_mapping(name: str, expected: Level) -> None:
    assert Level.from_name(name) is expected


@pytest.mark.parametrize("name", ["", "bogus", "verbose", "silly"])
def test_level_from_name_unknown(name: str) -> None:
    assert Level.from_name(name) is None


def test_level_ordering() -> None:
    assert Level.DEBUG < Level.INFO < Level.WARN < Level.ERROR


# ---------------------------------------------------------------------------
# jsonlog
# ---------------------------------------------------------------------------


def test_jsonlog_happy_path() -> None:
    line = json.dumps(
        {
            "ts": "2026-08-10T12:00:00.123Z",
            "level": "error",
            "msg": "boom",
            "host": "web01",
            "code": 5,
        }
    )
    result = jsonlog.parse(line, "app.log")
    assert result.level is Level.ERROR
    assert result.timestamp == datetime.fromisoformat("2026-08-10T12:00:00.123+00:00")
    assert result.message == "boom"
    assert result.fields["host"] == "web01"
    assert result.fields["code"] == "5"
    assert result.source == "app.log"
    assert result.raw == line


def test_jsonlog_epoch_seconds_timestamp() -> None:
    line = json.dumps({"time": 1723291200, "severity": "warn", "message": "slow"})
    result = jsonlog.parse(line, "app.log")
    assert result.level is Level.WARN
    assert result.timestamp is not None
    assert result.timestamp.year == 2024
    assert result.message == "slow"


def test_jsonlog_epoch_millis_timestamp() -> None:
    seconds_ts = 1723291200
    line = json.dumps({"timestamp": seconds_ts * 1000, "log.level": "info", "event": "started"})
    result = jsonlog.parse(line, "app.log")
    assert result.level is Level.INFO
    assert result.timestamp is not None
    assert result.timestamp.year == 2024
    assert result.message == "started"


def test_jsonlog_nested_field_is_compact_json() -> None:
    line = json.dumps({"msg": "hi", "extra": {"a": 1, "b": [1, 2]}})
    result = jsonlog.parse(line, "app.log")
    assert result.fields["extra"] == '{"a":1,"b":[1,2]}'


def test_jsonlog_first_present_key_wins() -> None:
    # "level" should be preferred over "lvl" per priority order; the unused
    # candidate key is not a recognized field, so it lands in fields as-is.
    line = json.dumps({"level": "warn", "lvl": "error", "msg": "x"})
    result = jsonlog.parse(line, "app.log")
    assert result.level is Level.WARN
    assert result.fields["lvl"] == "error"


def test_jsonlog_malformed_line_falls_back() -> None:
    result = jsonlog.parse("not json at all {{{", "app.log")
    assert result.level is None
    assert result.timestamp is None
    assert result.message == "not json at all {{{"
    assert result.raw == "not json at all {{{"


def test_jsonlog_non_dict_json_falls_back() -> None:
    result = jsonlog.parse("[1, 2, 3]", "app.log")
    assert result.level is None
    assert result.message == "[1, 2, 3]"


def test_jsonlog_empty_line() -> None:
    result = jsonlog.parse("", "app.log")
    assert result.message == ""
    assert result.raw == ""


def test_jsonlog_detect() -> None:
    sample = [json.dumps({"level": "info", "msg": "hi"}) for _ in range(4)] + ["plain text"]
    assert jsonlog.detect(sample) == pytest.approx(0.8)
    assert jsonlog.detect([]) == 0.0


# ---------------------------------------------------------------------------
# nginx
# ---------------------------------------------------------------------------

_ACCESS_LINE = (
    '10.0.0.1 - - [10/Aug/2026:12:00:00 +0000] "GET /api/users HTTP/1.1" '
    '{status} 612 "-" "curl/8.4.0"'
)


@pytest.mark.parametrize(
    ("status", "expected_level"),
    [
        (200, Level.INFO),
        (301, Level.INFO),
        (404, Level.WARN),
        (403, Level.WARN),
        (500, Level.ERROR),
        (502, Level.ERROR),
    ],
)
def test_nginx_access_level_mapping(status: int, expected_level: Level) -> None:
    line = _ACCESS_LINE.format(status=status)
    result = nginx.parse(line, "access.log")
    assert result.level is expected_level
    assert result.fields["status"] == str(status)
    assert result.fields["method"] == "GET"
    assert result.fields["path"] == "/api/users"
    assert result.fields["ip"] == "10.0.0.1"
    assert result.fields["bytes"] == "612"
    assert result.message == f"GET /api/users -> {status}"
    assert result.timestamp is not None
    assert result.timestamp.year == 2026
    assert result.timestamp.month == 8
    assert result.timestamp.day == 10


def test_nginx_error_log_line() -> None:
    line = "2026/08/10 12:00:00 [error] 123#0: *1 upstream timed out"
    result = nginx.parse(line, "error.log")
    assert result.level is Level.ERROR
    assert result.message == "upstream timed out"
    assert result.fields["pid"] == "123"
    assert result.fields["cid"] == "1"
    assert result.timestamp == datetime(2026, 8, 10, 12, 0, 0)


def test_nginx_error_log_without_connection_id() -> None:
    line = "2026/08/10 12:00:00 [warn] 123#0: something happened"
    result = nginx.parse(line, "error.log")
    assert result.level is Level.WARN
    assert result.message == "something happened"
    assert "cid" not in result.fields


def test_nginx_junk_line_falls_back() -> None:
    result = nginx.parse("this is not an nginx line", "x.log")
    assert result.level is None
    assert result.timestamp is None
    assert result.message == "this is not an nginx line"


def test_nginx_detect() -> None:
    sample = [_ACCESS_LINE.format(status=200)] * 3 + ["not nginx"]
    assert nginx.detect(sample) == pytest.approx(0.75)
    sample_errors = ["2026/08/10 12:00:00 [error] 1#0: boom"] * 2
    assert nginx.detect(sample_errors) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# syslog
# ---------------------------------------------------------------------------


def test_syslog_happy_path_with_pid() -> None:
    line = "Aug 10 12:00:00 web01 sshd[1234]: Failed password for root"
    result = syslog.parse(line, "syslog")
    assert result.fields["host"] == "web01"
    assert result.fields["proc"] == "sshd"
    assert result.fields["pid"] == "1234"
    assert result.level is Level.ERROR  # "Failed" token
    assert result.message == "Failed password for root"


def test_syslog_current_year_assumption() -> None:
    line = "Aug 10 12:00:00 web01 sshd[1234]: session opened"
    result = syslog.parse(line, "syslog")
    assert result.timestamp is not None
    assert result.timestamp.year == datetime.now().year
    assert result.timestamp.month == 8
    assert result.timestamp.day == 10
    assert result.timestamp.hour == 12


def test_syslog_without_pid() -> None:
    line = "Aug  9 09:03:11 web02 CRON: session opened"
    result = syslog.parse(line, "syslog")
    assert result.fields["proc"] == "CRON"
    assert "pid" not in result.fields
    assert result.level is None


@pytest.mark.parametrize(
    ("message", "expected_level"),
    [
        ("connection failed unexpectedly", Level.ERROR),
        ("disk usage warning threshold reached", Level.WARN),
        ("debug tracing enabled", Level.DEBUG),
        ("everything is fine", None),
    ],
)
def test_syslog_level_scan(message: str, expected_level: Level | None) -> None:
    line = f"Aug 10 12:00:00 web01 app[1]: {message}"
    result = syslog.parse(line, "syslog")
    assert result.level is expected_level


def test_syslog_junk_line_falls_back() -> None:
    result = syslog.parse("this does not look like syslog", "syslog")
    assert result.level is None
    assert result.timestamp is None
    assert result.message == "this does not look like syslog"


def test_syslog_detect_requires_month_start() -> None:
    sample = ["Aug 10 12:00:00 web01 sshd[1]: hi"] * 3
    assert syslog.detect(sample) == pytest.approx(1.0)
    # A line that merely contains a month name mid-string should not count.
    non_matching = ["scheduled for Aug 10 maintenance"] * 3
    assert syslog.detect(non_matching) == 0.0


# ---------------------------------------------------------------------------
# generic
# ---------------------------------------------------------------------------


def test_generic_comma_millis_timestamp_and_level() -> None:
    line = "2026-08-10 12:00:00,123 ERROR app.core: boom"
    result = generic.parse(line, "app.log")
    assert result.level is Level.ERROR
    assert result.timestamp == datetime(2026, 8, 10, 12, 0, 0, 123000)
    assert result.message == line
    assert result.continuation is False


def test_generic_bare_iso_timestamp_no_level() -> None:
    line = "2026-08-10T12:00:00 something happened"
    result = generic.parse(line, "app.log")
    assert result.timestamp == datetime(2026, 8, 10, 12, 0, 0)
    assert result.level is None


def test_generic_no_timestamp_no_level() -> None:
    line = "just a plain line with no structure"
    result = generic.parse(line, "app.log")
    assert result.timestamp is None
    assert result.level is None
    assert result.message == line


@pytest.mark.parametrize(
    "line",
    [
        "Traceback (most recent call last):",
        '  File "app/core/handler.py", line 10, in process_request',
        "    result = handler(item)",
        "at com.example.Handler.process(Handler.java:42)",
        "\tsome indented continuation",
    ],
)
def test_generic_continuation_lines(line: str) -> None:
    result = generic.parse(line, "app.log")
    assert result.continuation is True
    assert result.level is None
    assert result.message == line


def test_generic_empty_line() -> None:
    result = generic.parse("", "app.log")
    assert result.message == ""
    assert result.raw == ""
    assert result.continuation is False


def test_generic_detect_floor_and_boost() -> None:
    assert generic.detect([]) == 0.0
    assert generic.detect(["nothing structured here"]) == pytest.approx(0.1)
    structured = ["2026-08-10 12:00:00,000 INFO app: hi"] * 3
    assert generic.detect(structured) == pytest.approx(0.3)


# ---------------------------------------------------------------------------
# Malformed / junk input never raises, across all parsers
# ---------------------------------------------------------------------------

_JUNK_LINES = [
    "",
    "\x00\x01\x02 binary-ish junk",
    "{" * 50,
    "a" * 500,
    "🔥🔥🔥 emoji line with 日本語 mixed in 🔥🔥🔥",
    "   ",
    "\t\t\t",
    '{"unterminated": "json',
]


@pytest.mark.parametrize("module", [jsonlog, nginx, syslog, generic])
@pytest.mark.parametrize("line", _JUNK_LINES)
def test_parsers_never_raise_on_junk(module, line: str) -> None:
    result = module.parse(line, "junk.log")
    assert result.raw == line.rstrip("\n")
    assert isinstance(result.message, str)


# ---------------------------------------------------------------------------
# detect_format dispatcher
# ---------------------------------------------------------------------------


def test_detect_format_empty_sample_is_generic() -> None:
    assert detect_format([]) is generic


def test_detect_format_picks_json() -> None:
    sample = [json.dumps({"level": "info", "msg": f"line {i}"}) for i in range(5)]
    assert detect_format(sample) is jsonlog


def test_detect_format_picks_nginx() -> None:
    sample = [_ACCESS_LINE.format(status=200)] * 5
    assert detect_format(sample) is nginx


def test_detect_format_picks_syslog() -> None:
    sample = [f"Aug 10 12:00:0{i} web01 app[1]: message {i}" for i in range(5)]
    assert detect_format(sample) is syslog


def test_detect_format_picks_generic_for_generic_lines() -> None:
    sample = [f"2026-08-10 12:00:0{i},000 INFO app.core: message {i}" for i in range(5)]
    assert detect_format(sample) is generic


def test_detect_format_mixed_ambiguous_sample_falls_back_to_generic() -> None:
    sample = [
        "not any recognizable format",
        "another random line of text",
        "yet a third unrelated line",
    ]
    assert detect_format(sample) is generic


# ---------------------------------------------------------------------------
# sample_file / parser_for_file
# ---------------------------------------------------------------------------


def test_sample_file_reads_complete_nonempty_lines(tmp_path: Path) -> None:
    path = tmp_path / "sample.log"
    path.write_text("line one\n\nline two\nline three")
    lines = sample_file(str(path))
    assert lines == ["line one", "line two", "line three"]


def test_sample_file_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.log"
    path.write_text("")
    assert sample_file(str(path)) == []


def test_sample_file_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "does-not-exist.log"
    assert sample_file(str(path)) == []


def test_sample_file_drops_truncated_tail(tmp_path: Path) -> None:
    path = tmp_path / "trunc.log"
    path.write_text("a" * 20 + "\n" + "b" * 20 + "\n" + "c" * 20 + "\n")
    lines = sample_file(str(path), max_bytes=25)
    assert lines == ["a" * 20]


def test_parser_for_file_json(tmp_path: Path) -> None:
    path = tmp_path / "app.log"
    lines = [json.dumps({"level": "info", "msg": f"hi {i}"}) for i in range(5)]
    path.write_text("\n".join(lines) + "\n")
    assert parser_for_file(str(path)) is jsonlog


def test_parser_for_file_empty_is_generic(tmp_path: Path) -> None:
    path = tmp_path / "empty.log"
    path.write_text("")
    assert parser_for_file(str(path)) is generic


# ---------------------------------------------------------------------------
# Integration sanity check via scripts/genlog.py
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_GENLOG = _REPO_ROOT / "scripts" / "genlog.py"

_EXPECTED_MODULE = {
    "json": jsonlog,
    "syslog": syslog,
    "nginx": nginx,
    "generic": generic,
}


@pytest.mark.parametrize("fmt", ["json", "syslog", "nginx", "generic"])
def test_genlog_output_detected_correctly(tmp_path: Path, fmt: str) -> None:
    output = tmp_path / f"{fmt}.log"
    subprocess.run(
        [
            sys.executable,
            str(_GENLOG),
            str(output),
            "--format",
            fmt,
            "--count",
            "40",
            "--rate",
            "0",
            "--seed",
            "7",
            "--error-rate",
            "0.2",
        ],
        check=True,
        cwd=_REPO_ROOT,
    )
    assert parser_for_file(str(output)) is _EXPECTED_MODULE[fmt]
