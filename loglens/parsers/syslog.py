"""Parser for RFC 3164 style syslog lines."""

from __future__ import annotations

import re
from datetime import datetime

from loglens.parsers import Level, LogLine

NAME = "syslog"

_HEADER_RE = re.compile(
    r"^(?P<month>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+"
    r"(?P<day>\d{1,2})\s+"
    r"(?P<time>\d{2}:\d{2}:\d{2})\s+"
    r"(?P<host>\S+)\s+"
    r"(?P<proc>[^\[\s:]+)(?:\[(?P<pid>\d+)\])?:\s*"
    r"(?P<message>.*)$"
)

_LEVEL_TOKENS = (
    (re.compile(r"\b(?:error|fail|failed|failure)\b", re.IGNORECASE), Level.ERROR),
    (re.compile(r"\b(?:warn|warning)\b", re.IGNORECASE), Level.WARN),
    (re.compile(r"\bdebug\b", re.IGNORECASE), Level.DEBUG),
)


def detect(sample: list[str]) -> float:
    if not sample:
        return 0.0
    hits = sum(1 for line in sample if _HEADER_RE.match(line))
    return hits / len(sample)


def _parse_timestamp(month: str, day: str, time_str: str) -> datetime | None:
    year = datetime.now().year
    try:
        return datetime.strptime(f"{year} {month} {int(day):02d} {time_str}", "%Y %b %d %H:%M:%S")
    except ValueError:
        return None


def _scan_level(message: str) -> Level | None:
    for pattern, level in _LEVEL_TOKENS:
        if pattern.search(message):
            return level
    return None


def parse(raw: str, source: str) -> LogLine:
    line = raw.rstrip("\n")
    if not line:
        return LogLine(source=source, raw="", message="")

    try:
        match = _HEADER_RE.match(line)
        if not match:
            return LogLine(source=source, raw=line, message=line)

        message = match.group("message")
        fields = {"host": match.group("host"), "proc": match.group("proc")}
        pid = match.group("pid")
        if pid:
            fields["pid"] = pid

        return LogLine(
            source=source,
            raw=line,
            message=message,
            timestamp=_parse_timestamp(
                match.group("month"), match.group("day"), match.group("time")
            ),
            level=_scan_level(message),
            fields=fields,
        )
    except Exception:
        return LogLine(source=source, raw=line, message=line)
