"""Parser for line-delimited JSON logs (one JSON object per line)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from loglens.parsers import Level, LogLine

NAME = "json"

_LEVEL_KEYS = ("level", "lvl", "severity", "log.level")
_TIME_KEYS = ("ts", "time", "timestamp", "@timestamp")
_MESSAGE_KEYS = ("msg", "message", "event")


def detect(sample: list[str]) -> float:
    if not sample:
        return 0.0
    hits = 0
    for line in sample:
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            hits += 1
    return hits / len(sample)


def _parse_timestamp(value: object) -> datetime | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000.0 if value > 1e12 else value
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None
    return None


def _first_present(obj: dict, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        if key in obj and obj[key] is not None:
            return key
    return None


def parse(raw: str, source: str) -> LogLine:
    line = raw.rstrip("\n")
    if not line:
        return LogLine(source=source, raw="", message="")

    try:
        obj = json.loads(line)
        if not isinstance(obj, dict):
            return LogLine(source=source, raw=line, message=line)

        level_key = _first_present(obj, _LEVEL_KEYS)
        level = Level.from_name(str(obj[level_key])) if level_key else None

        time_key = _first_present(obj, _TIME_KEYS)
        timestamp = _parse_timestamp(obj[time_key]) if time_key else None

        msg_key = _first_present(obj, _MESSAGE_KEYS)
        message = str(obj[msg_key]) if msg_key else line

        consumed = {key for key in (level_key, time_key, msg_key) if key is not None}
        fields: dict[str, str] = {}
        for key, value in obj.items():
            if key in consumed:
                continue
            if isinstance(value, (dict, list)):
                fields[key] = json.dumps(value, separators=(",", ":"))
            else:
                fields[key] = str(value)

        return LogLine(
            source=source,
            raw=line,
            message=message,
            timestamp=timestamp,
            level=level,
            fields=fields,
        )
    except Exception:
        return LogLine(source=source, raw=line, message=line)
