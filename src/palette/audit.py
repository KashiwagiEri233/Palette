"""Minimal, bounded audit logging.

The plan's rule is narrow on purpose: record the *shape* of what happened — action type,
input length, timing, outcome, error code — and never the sensitive payload. Typed text,
launch arguments and screenshot contents therefore never reach this file, and neither do
host paths (only the managed, relative object name).

Storage is newline-delimited JSON with size-based rotation, so a runaway agent cannot
fill the disk with audit records, and no database is required for the MVP.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import queue
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, TextIO

logger = logging.getLogger("palette.audit")
_MAX_ASYNC_QUEUE = 4096

#: Keys that must never be written, whatever a caller passes.
_FORBIDDEN_KEYS = frozenset(
    {
        "text",
        "password",
        "token",
        "secret",
        "arguments",
        "options",
        "path",
        "argv",
        "env",
        "filename",
        "image",
        "base64",
    }
)


def _scrub(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key.lower() not in _FORBIDDEN_KEYS}


class AuditLog:
    """Append-only bounded JSONL writer.

    Writes are synchronous and short. A failure to write an audit record never fails the
    operation it describes: the service logs a warning and carries on, because losing an
    audit line must not turn into an outage for the agent.
    """

    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int = 8 * 1024 * 1024,
        max_files: int = 5,
        enabled: bool = True,
    ) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self.max_files = max_files
        self.enabled = enabled
        self._lock = threading.Lock()
        self._queue_lock = threading.Lock()
        self._failures = 0
        self._handle: TextIO | None = None
        self._size = 0
        self._dir_ready = False
        self._queue: queue.SimpleQueue[str | threading.Event | None] | None = None
        self._worker: threading.Thread | None = None
        self._pending_count = 0
        self._dropped = 0
        self._closed = False

    # -- writing ---------------------------------------------------------------------
    def _format_line(self, event: str, fields: dict[str, Any]) -> str:
        payload = _scrub({"ts": _iso_now(), "event": event, **fields})
        return json.dumps(payload, separators=(",", ":"), default=str) + "\n"

    def _close_handle_locked(self) -> None:
        if self._handle is not None:
            with contextlib.suppress(OSError):
                self._handle.close()
            self._handle = None
            self._size = 0

    def _write_lines_locked(self, lines: list[str]) -> None:
        try:
            if not self._dir_ready:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._dir_ready = True
            for line in lines:
                if self._handle is None:
                    self._rotate_if_needed()
                    self._handle = self.path.open("a", encoding="utf-8", newline="\n")
                    self._size = self._handle.tell()
                elif self._size >= self.max_bytes:
                    self._close_handle_locked()
                    self._rotate_if_needed()
                    self._handle = self.path.open("a", encoding="utf-8", newline="\n")
                    self._size = self._handle.tell()
                self._handle.write(line)
                self._size += len(line.encode("utf-8"))
            if self._handle is not None:
                self._handle.flush()
        except OSError as exc:  # pragma: no cover - disk failure path
            self._close_handle_locked()
            self._dir_ready = False
            self._failures += 1
            logger.warning("audit write failed (%s): %s", self._failures, exc)

    def _run_writer(self, work_queue: queue.SimpleQueue[str | threading.Event | None]) -> None:
        while True:
            item = work_queue.get()
            lines: list[str] = []
            barriers: list[threading.Event] = []
            stop = False
            while True:
                if item is None:
                    stop = True
                elif isinstance(item, threading.Event):
                    barriers.append(item)
                else:
                    lines.append(item)
                try:
                    item = work_queue.get_nowait()
                except queue.Empty:
                    break
            if lines:
                with self._queue_lock:
                    self._pending_count = max(0, self._pending_count - len(lines))
                with self._lock:
                    self._write_lines_locked(lines)
            for barrier in barriers:
                barrier.set()
            if stop:
                return

    def record(self, event: str, **fields: Any) -> None:
        if not self.enabled:
            return
        line = self._format_line(event, fields)
        with self._lock:
            self._write_lines_locked([line])

    def record_async(self, event: str, **fields: Any) -> None:
        if not self.enabled:
            return
        line = self._format_line(event, fields)
        with self._queue_lock:
            if self._closed:
                return
            if self._pending_count >= _MAX_ASYNC_QUEUE:
                self._dropped += 1
                return
            if self._queue is None:
                self._queue = queue.SimpleQueue()
            if self._worker is None:
                self._worker = threading.Thread(
                    target=self._run_writer,
                    args=(self._queue,),
                    name="palette-audit-writer",
                    daemon=True,
                )
                self._worker.start()
            self._pending_count += 1
            self._queue.put(line)

    def flush(self, timeout: float = 2.0) -> None:
        with self._queue_lock:
            worker = self._worker
            queue_ref = self._queue
            if worker is None or not worker.is_alive() or queue_ref is None:
                return
            barrier = threading.Event()
            queue_ref.put(barrier)
        barrier.wait(timeout)

    def close(self, timeout: float = 2.0) -> None:
        with self._queue_lock:
            self._closed = True
            worker = self._worker
            queue_ref = self._queue
            if worker is not None and worker.is_alive() and queue_ref is not None:
                queue_ref.put(None)
        if worker is not None and worker.is_alive():
            worker.join(timeout)
        with self._lock:
            self._close_handle_locked()

    def _rotate_if_needed(self) -> None:
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return
        if size < self.max_bytes:
            return
        for index in range(self.max_files - 1, 0, -1):
            source = self.path if index == 1 else self.path.with_suffix(f".{index - 1}.jsonl")
            target = self.path.with_suffix(f".{index}.jsonl")
            if source.exists():
                os.replace(source, target)
        # The oldest generation is dropped rather than archived indefinitely.
        oldest = self.path.with_suffix(f".{self.max_files - 1}.jsonl")
        if oldest.exists() and self.max_files > 1:
            oldest.unlink(missing_ok=True)
        if self.max_files == 1:
            self.path.unlink(missing_ok=True)

    # -- reading (used by tests and the doctor command) -------------------------------
    def tail(self, limit: int = 50) -> list[dict[str, Any]]:
        self.flush()
        records: deque[dict[str, Any]] = deque(maxlen=limit)
        with self._lock:
            if not self.path.exists():
                return []
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return list(records)


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()) + _offset()


def _offset() -> str:
    offset = -time.timezone if not time.localtime().tm_isdst else -time.altzone
    sign = "+" if offset >= 0 else "-"
    hours, remainder = divmod(abs(offset), 3600)
    return f"{sign}{hours:02d}:{remainder // 60:02d}"


class NullAuditLog(AuditLog):
    """Audit sink for tests and for profiles that disable auditing."""

    def __init__(self) -> None:
        super().__init__(Path(os.devnull), enabled=False)
