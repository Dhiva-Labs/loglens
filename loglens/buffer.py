"""Ring buffer of log lines with sequence numbers and search indexing.

LineBuffer holds up to `capacity` lines, oldest evicted first once full. Every
appended line gets a monotonic sequence number (starting at 1) that survives
eviction, so callers can hold onto a seq and later ask "is this still here."

Appends happen from the tailer's background thread; reads happen from the
Textual app thread. Every public method takes a plain lock, so the buffer is
safe to share between the two without the caller doing anything special.

LogLine is a slots dataclass and can't carry extra attributes, so the derived
"effective level" used for error filtering (continuation lines inherit the
level of the most recent real line from the same source) is tracked here in a
small internal record instead.
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import islice

from loglens.parsers import Level, LogLine


@dataclass(slots=True)
class _Entry:
    """One buffered line plus the bookkeeping the buffer needs around it."""

    seq: int
    line: LogLine
    effective_level: Level | None


class LineBuffer:
    """Thread-safe fixed-capacity ring buffer of LogLine records."""

    def __init__(self, capacity: int = 100_000) -> None:
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self._capacity = capacity
        self._entries: deque[_Entry] = deque(maxlen=capacity)
        self._seq = 0
        # Most recent effective level seen per source, updated on every
        # non-continuation append. Kept independent of what's still in the
        # deque, so a continuation line still attaches correctly to a parent
        # that has since been evicted.
        self._last_level: dict[str, Level | None] = {}
        self._lock = threading.Lock()

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def total_appended(self) -> int:
        with self._lock:
            return self._seq

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def append(self, line: LogLine) -> int:
        with self._lock:
            return self._append_locked(line)

    def extend(self, lines: Iterable[LogLine]) -> None:
        with self._lock:
            for line in lines:
                self._append_locked(line)

    def _append_locked(self, line: LogLine) -> int:
        if line.arrival == 0.0:
            line.arrival = time.time()

        self._seq += 1
        seq = self._seq

        source = line.source
        if line.continuation:
            effective_level = self._last_level.get(source)
        else:
            effective_level = line.level
            self._last_level[source] = line.level

        self._entries.append(_Entry(seq=seq, line=line, effective_level=effective_level))
        return seq

    def get(self, seq: int) -> LogLine | None:
        with self._lock:
            if not self._entries:
                return None
            first_seq = self._entries[0].seq
            last_seq = self._entries[-1].seq
            if seq < first_seq or seq > last_seq:
                return None
            return self._entries[seq - first_seq].line

    def since(self, seq: int) -> list[tuple[int, LogLine, Level | None]]:
        """Entries with seq strictly greater than `seq`, oldest first, with each
        entry's effective level (continuation lines inherit their parent's level).

        O(new) via first-seq arithmetic.
        """
        with self._lock:
            if not self._entries:
                return []
            first_seq = self._entries[0].seq
            last_seq = self._entries[-1].seq
            if seq >= last_seq:
                return []
            # Seqs of live entries are contiguous, so the count of entries
            # newer than `seq` falls straight out of the endpoints - no scan
            # needed to find where "new" starts.
            count = last_seq - max(seq, first_seq - 1)
            newest = list(islice(reversed(self._entries), count))
            newest.reverse()
            return [(entry.seq, entry.line, entry.effective_level) for entry in newest]

    def snapshot(self) -> list[tuple[int, LogLine]]:
        with self._lock:
            return [(entry.seq, entry.line) for entry in self._entries]

    def view(
        self,
        *,
        pattern: re.Pattern[str] | None = None,
        errors_only: bool = False,
        limit: int | None = None,
    ) -> list[tuple[int, LogLine]]:
        with self._lock:
            entries = list(self._entries)

        def matches(entry: _Entry) -> bool:
            if errors_only and entry.effective_level != Level.ERROR:
                return False
            return pattern is None or pattern.search(entry.line.raw) is not None

        if limit is None:
            return [(entry.seq, entry.line) for entry in entries if matches(entry)]
        if limit <= 0:
            return []

        matched: list[tuple[int, LogLine]] = []
        for entry in reversed(entries):
            if matches(entry):
                matched.append((entry.seq, entry.line))
                if len(matched) >= limit:
                    break
        matched.reverse()
        return matched

    def search(self, pattern: re.Pattern[str]) -> list[int]:
        with self._lock:
            entries = list(self._entries)
        return [entry.seq for entry in entries if pattern.search(entry.line.raw)]

    def error_counts(
        self, *, bucket_seconds: int = 60, buckets: int = 15, now: float | None = None
    ) -> list[int]:
        if bucket_seconds < 1:
            raise ValueError("bucket_seconds must be at least 1")
        if buckets < 1:
            raise ValueError("buckets must be at least 1")

        reference = time.time() if now is None else now
        window_start = reference - bucket_seconds * buckets
        counts = [0] * buckets

        with self._lock:
            entries = list(self._entries)

        # Newest first: arrivals rise with append order, so the first entry
        # older than the window means everything before it is too.
        for entry in reversed(entries):
            arrival = entry.line.arrival
            if arrival < window_start:
                break
            if arrival > reference:
                continue
            if entry.line.continuation:
                continue
            if entry.effective_level != Level.ERROR:
                continue
            bucket_index = int((arrival - window_start) // bucket_seconds)
            bucket_index = min(bucket_index, buckets - 1)
            counts[bucket_index] += 1

        return counts
