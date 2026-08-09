"""Parser for nginx access (combined) and error log lines."""

from __future__ import annotations

import re
from datetime import datetime
from re import Match

from loglens.parsers import Level, LogLine

NAME = "nginx"

_ACCESS_RE = re.compile(
    r"^(?P<ip>\S+) \S+ \S+ \[(?P<time>[^\]]+)\] "
    r'"(?P<method>[A-Z]+) (?P<path>\S+)(?: [^"]*)?" '
    r"(?P<status>\d{3}) (?P<bytes>\S+)"
    r'(?: "(?P<referer>[^"]*)" "(?P<agent>[^"]*)")?'
)

_ERROR_RE = re.compile(
    r"^(?P<time>\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}) "
    r"\[(?P<level>\w+)\] "
    r"(?P<pid>\d+)#(?P<tid>\d+): "
    r"(?:\*(?P<cid>\d+) )?"
    r"(?P<message>.*)$"
)


def detect(sample: list[str]) -> float:
    if not sample:
        return 0.0
    hits = sum(1 for line in sample if _ACCESS_RE.match(line) or _ERROR_RE.match(line))
    return hits / len(sample)


def _parse_access_time(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%d/%b/%Y:%H:%M:%S %z")
    except ValueError:
        return None


def _parse_error_time(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y/%m/%d %H:%M:%S")
    except ValueError:
        return None


def _parse_access(match: Match[str], line: str, source: str) -> LogLine:
    status_str = match.group("status")
    try:
        status = int(status_str)
    except ValueError:
        status = None

    if status is not None and status >= 500:
        level = Level.ERROR
    elif status is not None and status >= 400:
        level = Level.WARN
    else:
        level = Level.INFO

    method = match.group("method")
    path = match.group("path")
    fields = {
        "status": status_str,
        "method": method,
        "path": path,
        "ip": match.group("ip"),
        "bytes": match.group("bytes"),
    }
    message = f"{method} {path} -> {status_str}"

    return LogLine(
        source=source,
        raw=line,
        message=message,
        timestamp=_parse_access_time(match.group("time")),
        level=level,
        fields=fields,
    )


def _parse_error(match: Match[str], line: str, source: str) -> LogLine:
    fields = {"pid": match.group("pid")}
    cid = match.group("cid")
    if cid:
        fields["cid"] = cid

    return LogLine(
        source=source,
        raw=line,
        message=match.group("message"),
        timestamp=_parse_error_time(match.group("time")),
        level=Level.from_name(match.group("level")),
        fields=fields,
    )


def parse(raw: str, source: str) -> LogLine:
    line = raw.rstrip("\n")
    if not line:
        return LogLine(source=source, raw="", message="")

    try:
        access_match = _ACCESS_RE.match(line)
        if access_match:
            return _parse_access(access_match, line, source)

        error_match = _ERROR_RE.match(line)
        if error_match:
            return _parse_error(error_match, line, source)

        return LogLine(source=source, raw=line, message=line)
    except Exception:
        return LogLine(source=source, raw=line, message=line)
