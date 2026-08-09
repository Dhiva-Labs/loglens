"""Log format parsers and format auto-detection.

Each parser submodule (jsonlog, nginx, syslog, generic) exposes a NAME, a
detect(sample) confidence score, and a parse(raw, source) function that
turns a single raw line into a LogLine. detect_format() picks the best
parser for a sample of lines from a file; generic always serves as the
fallback.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import ModuleType


class Level(enum.IntEnum):
    """Log severity, ordered low to high."""

    DEBUG = 10
    INFO = 20
    WARN = 30
    ERROR = 40

    @classmethod
    def from_name(cls, name: str) -> Level | None:
        """Map a level name found in a log line to a Level, case-insensitively."""
        if not name:
            return None
        key = name.strip().upper()
        if key in ("TRACE", "DEBUG"):
            return cls.DEBUG
        if key in ("INFO", "NOTICE"):
            return cls.INFO
        if key in ("WARN", "WARNING"):
            return cls.WARN
        if key in ("ERROR", "ERR", "CRITICAL", "CRIT", "FATAL", "ALERT", "EMERG"):
            return cls.ERROR
        return None


@dataclass(slots=True)
class LogLine:
    """A single parsed log record."""

    source: str
    raw: str
    message: str
    timestamp: datetime | None = None
    level: Level | None = None
    fields: dict[str, str] = field(default_factory=dict)
    continuation: bool = False
    arrival: float = 0.0


# Imported after Level/LogLine are defined: each parser submodule imports
# those two names back from this package at module load time.
from loglens.parsers import generic, jsonlog, nginx, syslog  # noqa: E402

PARSERS: tuple[ModuleType, ...] = (jsonlog, nginx, syslog, generic)


def detect_format(sample: list[str]) -> ModuleType:
    """Pick the parser module with the highest confidence for this sample."""
    if not sample:
        return generic
    best_module: ModuleType = generic
    best_confidence = -1.0
    for module in PARSERS:
        confidence = module.detect(sample)
        if confidence > best_confidence:
            best_confidence = confidence
            best_module = module
    return best_module


def sample_file(path: str | Path, max_bytes: int = 8192) -> list[str]:
    """Read up to max_bytes from the head of path, returning complete non-empty lines."""
    try:
        with Path(path).open("rb") as handle:
            data = handle.read(max_bytes)
    except OSError:
        return []
    text = data.decode("utf-8", errors="replace")
    lines = text.split("\n")
    if len(data) >= max_bytes and lines:
        # The final fragment may have been cut off mid-line by the byte cap.
        lines = lines[:-1]
    return [stripped for raw_line in lines if (stripped := raw_line.rstrip("\r"))]


def parser_for_file(path: str | Path) -> ModuleType:
    """Sample a file and return the parser module best suited to it."""
    return detect_format(sample_file(path))
