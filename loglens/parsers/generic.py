"""Fallback parser for log lines that don't match a more specific format."""

from __future__ import annotations

import re
from datetime import datetime

from loglens.parsers import Level, LogLine

NAME = "generic"

_TS_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?(?:Z|[+-]\d{2}:?\d{2})?)"
)

_LEVEL_RE = re.compile(
    r"\b(DEBUG|INFO|NOTICE|WARNING|WARN|ERROR|CRITICAL|CRIT|FATAL|TRACE)\b",
    re.IGNORECASE,
)

_CONTINUATION_PREFIXES = ("Traceback (most recent call last)", "  File ", "at ")


def detect(sample: list[str]) -> float:
    if not sample:
        return 0.0
    hits = sum(1 for line in sample if _TS_RE.match(line) or _LEVEL_RE.search(line))
    if hits / len(sample) > 0.5:
        return 0.3
    return 0.1


def _is_continuation(line: str) -> bool:
    if not line:
        return False
    if line[0] in (" ", "\t"):
        return True
    return line.startswith(_CONTINUATION_PREFIXES)


def _extract_timestamp(line: str) -> datetime | None:
    match = _TS_RE.match(line)
    if not match:
        return None
    text = match.group("ts").replace(",", ".")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def parse(raw: str, source: str) -> LogLine:
    line = raw.rstrip("\n")
    if not line:
        return LogLine(source=source, raw="", message="")

    try:
        if _is_continuation(line):
            return LogLine(source=source, raw=line, message=line, continuation=True)

        timestamp = _extract_timestamp(line)
        level_match = _LEVEL_RE.search(line)
        level = Level.from_name(level_match.group(1)) if level_match else None

        return LogLine(
            source=source,
            raw=line,
            message=line,
            timestamp=timestamp,
            level=level,
        )
    except Exception:
        return LogLine(source=source, raw=line, message=line)
