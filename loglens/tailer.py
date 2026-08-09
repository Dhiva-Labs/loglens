"""Multi-file log tailing with rotation handling.

A single background worker thread services every tailed file: each cycle it
stats each path and only reads bytes when something actually changed
(growth, truncation, or the inode changing under a logrotate-style
rename+recreate). The worker sleeps on a threading.Event with a timeout of
poll_interval, so it never busy-spins; an optional watchdog observer wakes
it promptly on filesystem events, and the timeout keeps things moving even
when use_watchdog=False or watchdog misses an event.

This module has no framework dependencies beyond watchdog and the standard
library, so it can be reused by any front end.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

_JOIN_TIMEOUT = 5.0


@dataclass(slots=True)
class _FileState:
    """Per-file bookkeeping the worker thread mutates each cycle."""

    path: str
    handle: BinaryIO | None = None
    inode: int | None = None
    device: int | None = None
    position: int = 0
    pending: bytes = b""


def _event_path(raw: str | bytes) -> str:
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    return os.path.abspath(text)


class _WakeHandler(FileSystemEventHandler):
    """Sets a threading.Event when a watched path is created/modified/moved."""

    def __init__(self, watched_paths: set[str], wake_event: threading.Event) -> None:
        super().__init__()
        self._watched_paths = watched_paths
        self._wake_event = wake_event

    def _maybe_wake(self, raw_path: str | bytes) -> None:
        if _event_path(raw_path) in self._watched_paths:
            self._wake_event.set()

    def on_created(self, event: FileSystemEvent) -> None:
        self._maybe_wake(event.src_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        self._maybe_wake(event.src_path)

    def on_deleted(self, event: FileSystemEvent) -> None:
        self._maybe_wake(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        self._maybe_wake(event.src_path)
        dest_path = getattr(event, "dest_path", "")
        if dest_path:
            self._maybe_wake(dest_path)


class MultiTailer:
    """Tails a fixed set of files, delivering complete new lines as they appear."""

    def __init__(
        self,
        paths: Sequence[str | Path],
        on_lines: Callable[[str, list[str]], None],
        *,
        poll_interval: float = 0.25,
        from_start: bool = False,
        use_watchdog: bool = True,
    ) -> None:
        self._on_lines = on_lines
        self._poll_interval = poll_interval
        self._from_start = from_start
        self._use_watchdog = use_watchdog

        self._states = [_FileState(path=str(p)) for p in paths]
        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._observer: Observer | None = None
        self._lock = threading.Lock()
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._stop_event.clear()
            self._wake_event.clear()

            for state in self._states:
                self._open_initial(state)

            if self._use_watchdog:
                self._observer = self._start_observer()

            worker = threading.Thread(target=self._run, name="loglens-tailer", daemon=True)
            self._worker = worker
            self._running = True
            worker.start()

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            self._stop_event.set()
            self._wake_event.set()
            worker = self._worker
            observer = self._observer
            self._worker = None
            self._observer = None
            self._running = False

        if observer is not None:
            try:
                observer.stop()
                if threading.current_thread() is not observer:
                    observer.join(timeout=_JOIN_TIMEOUT)
            except Exception:
                pass

        if worker is not None and threading.current_thread() is not worker:
            worker.join(timeout=_JOIN_TIMEOUT)

        for state in self._states:
            self._close_handle(state)

    # -- observer setup ----------------------------------------------------

    def _start_observer(self) -> Observer | None:
        watched_paths = {os.path.abspath(state.path) for state in self._states}
        directories = {os.path.dirname(p) for p in watched_paths if os.path.dirname(p)}
        handler = _WakeHandler(watched_paths, self._wake_event)

        observer = Observer()
        scheduled = False
        for directory in directories:
            try:
                observer.schedule(handler, directory, recursive=False)
                scheduled = True
            except OSError:
                continue

        if not scheduled:
            return None

        observer.daemon = True
        observer.start()
        return observer

    # -- worker loop ---------------------------------------------------------

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self._wake_event.wait(self._poll_interval)
            self._wake_event.clear()
            if self._stop_event.is_set():
                break
            for state in self._states:
                try:
                    self._process_one(state)
                except Exception:
                    continue

    # -- per-file handling -----------------------------------------------

    def _open(self, state: _FileState) -> os.stat_result | None:
        """Open state.path fresh, refreshing handle/inode/device. None on failure."""
        try:
            handle = open(state.path, "rb")
            fst = os.fstat(handle.fileno())
        except OSError:
            return None
        state.handle = handle
        state.inode = fst.st_ino
        state.device = fst.st_dev
        return fst

    def _close_handle(self, state: _FileState) -> None:
        if state.handle is not None:
            try:
                state.handle.close()
            except OSError:
                pass
            state.handle = None

    def _open_initial(self, state: _FileState) -> None:
        fst = self._open(state)
        if fst is None:
            return
        state.position = 0 if self._from_start else fst.st_size
        state.pending = b""

    def _forget(self, state: _FileState) -> None:
        """Drop a file that has gone missing so it reopens from 0 if it returns."""
        self._close_handle(state)
        state.inode = None
        state.device = None
        state.position = 0
        state.pending = b""

    def _process_one(self, state: _FileState) -> None:
        try:
            st = os.stat(state.path)
        except OSError:
            self._forget(state)
            return

        if state.handle is None:
            if self._open(state) is None:
                return
            state.position = 0
            state.pending = b""
        else:
            same_file = (st.st_ino, st.st_dev) == (state.inode, state.device)
            if not same_file:
                self._close_handle(state)
                if self._open(state) is None:
                    return
                state.position = 0
                state.pending = b""
            elif st.st_size < state.position:
                state.position = 0
                state.pending = b""
                try:
                    state.handle.seek(0)
                except OSError:
                    pass

        if st.st_size <= state.position or state.handle is None:
            return

        try:
            state.handle.seek(state.position)
            data = state.handle.read(st.st_size - state.position)
        except OSError:
            return
        if not data:
            return

        state.position += len(data)
        combined = state.pending + data
        parts = combined.split(b"\n")
        state.pending = parts.pop()
        if not parts:
            return

        lines = [self._decode_line(part) for part in parts]
        self._emit(state.path, lines)

    @staticmethod
    def _decode_line(raw: bytes) -> str:
        return raw.decode("utf-8", errors="replace").rstrip("\r")

    def _emit(self, source: str, lines: list[str]) -> None:
        try:
            self._on_lines(source, lines)
        except Exception:
            # A misbehaving callback must never take the worker thread down.
            pass


def read_tail(path: str | Path, max_lines: int = 200, max_bytes: int = 65536) -> list[str]:
    """Last complete lines of a file (for initial scrollback preload). Never raises; [] on error."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            read_size = min(size, max_bytes)
            handle.seek(size - read_size)
            data = handle.read(read_size)
    except OSError:
        return []

    if not data:
        return []

    if read_size < size:
        # We started mid-file; the first fragment is likely a partial line.
        newline_index = data.find(b"\n")
        if newline_index == -1:
            return []
        data = data[newline_index + 1 :]

    lines = data.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()

    lines = lines[-max_lines:]
    return [line.decode("utf-8", errors="replace").rstrip("\r") for line in lines]
