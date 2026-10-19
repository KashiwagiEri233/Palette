"""Instance-bound idempotency records for action transactions."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import Any

from ..errors import ErrorCode, PaletteError
from .state import ActionResult, RequestKey


class RequestDeduplicator:
    """``request_id`` is an application-level idempotency key, not a QMP or JSON-RPC id.

    A replay with identical parameters returns the original receipt; a replay with
    different parameters is a conflict. Only in-process, bounded memory is promised —
    exactly-once across a process restart is out of scope, and no log can undo input that
    already reached the guest.
    """

    def __init__(self, *, max_entries: int = 256) -> None:
        self._records: OrderedDict[RequestKey, tuple[str, ActionResult]] = OrderedDict()
        self._inflight: dict[RequestKey, tuple[str, asyncio.Future[Any]]] = {}
        self._max_entries = max_entries

    def lookup(self, request_id: RequestKey, fingerprint: str) -> ActionResult | None:
        record = self._records.get(request_id)
        if record is None:
            return None
        stored_fingerprint, receipt = record
        if stored_fingerprint != fingerprint:
            raise PaletteError(
                ErrorCode.REQUEST_ID_CONFLICT,
                "this request_id was already used with different parameters",
                details={"request_id": request_id},
            )
        return receipt.model_copy(update={"deduplicated": True})

    def begin(self, request_id: RequestKey, fingerprint: str) -> asyncio.Future[Any] | None:
        """Return an existing in-flight future for an identical concurrent request."""
        existing = self._inflight.get(request_id)
        if existing is None:
            return None
        stored_fingerprint, future = existing
        if stored_fingerprint != fingerprint:
            raise PaletteError(
                ErrorCode.REQUEST_ID_CONFLICT,
                "this request_id is already running with different parameters",
                details={"request_id": request_id},
            )
        return future

    def attach(self, request_id: RequestKey, fingerprint: str, future: asyncio.Future[Any]) -> None:
        self._inflight[request_id] = (fingerprint, future)

    def complete(self, request_id: RequestKey, fingerprint: str, receipt: ActionResult) -> None:
        self._inflight.pop(request_id, None)
        self._records[request_id] = (fingerprint, receipt)
        while len(self._records) > self._max_entries:
            self._records.popitem(last=False)

    def abandon(self, request_id: RequestKey) -> None:
        self._inflight.pop(request_id, None)
