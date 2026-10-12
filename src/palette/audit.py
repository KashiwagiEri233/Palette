"""Minimal, bounded audit logging.

The plan's rule is narrow on purpose: record the *shape* of what happened — action type,
input length, timing, outcome, error code — and never the sensitive payload. Typed text,
launch arguments and screenshot contents therefore never reach this file, and neither do
host paths (only the managed, relative object name).

Storage is newline-delimited JSON with size-based rotation, so a runaway agent cannot
fill the disk with audit records, and no database is required for the MVP.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

logger = logging.getLogger("palette.audit")

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
        self._failures = 0

    # -- writing ---------------------------------------------------------------------
    def record(self, event: str, **fields: Any) -> None:
        if not self.enabled:
            return
        payload = _scrub({"ts": _iso_now(), "event": event, **fields})
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._rotate_if_needed()
                with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(json.dumps(payload, separators=(",", ":"), default=str) + "\n")
        except OSError as exc:  # pragma: no cover - disk failure path
            self._failures += 1
            logger.warning("audit write failed (%s): %s", self._failures, exc)

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
        records: deque[dict[str, Any]] = deque(maxlen=limit)
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
