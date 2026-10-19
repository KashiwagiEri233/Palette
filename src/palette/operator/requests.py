"""Bounded in-process request ownership shared by authenticated IPC adapters."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable, Coroutine
from typing import Any

from ..errors import ErrorCode, PaletteError


class RequestRegistry[T]:
    def __init__(self, *, pending_limit: int, retained_limit: int) -> None:
        self.pending_limit = pending_limit
        self.retained_limit = retained_limit
        self.entries: OrderedDict[str, tuple[str, asyncio.Task[T]]] = OrderedDict()
        self.closing = False

    async def execute(
        self, request_id: str, fingerprint: str, run: Callable[[], Coroutine[Any, Any, T]]
    ) -> T:
        if self.closing:
            raise PaletteError(ErrorCode.BUSY, "the IPC server is stopping")
        cached = self.entries.get(request_id)
        if cached is not None:
            if cached[0] != fingerprint:
                raise PaletteError(
                    ErrorCode.REQUEST_ID_CONFLICT, "request_id has different arguments"
                )
            return await asyncio.shield(cached[1])
        if sum(not task.done() for _, task in self.entries.values()) >= self.pending_limit:
            raise PaletteError(ErrorCode.BUSY, "the IPC request limit has been reached")
        task = asyncio.create_task(run(), name="ipc-operation")
        self.entries[request_id] = (fingerprint, task)
        task.add_done_callback(self._completed)
        return await asyncio.shield(task)

    def _completed(self, completed: asyncio.Task[T]) -> None:
        if not completed.cancelled():
            completed.exception()
        for key, (_, task) in list(self.entries.items()):
            if len(self.entries) <= self.retained_limit:
                break
            if task.done():
                self.entries.pop(key, None)

    async def close(self) -> None:
        self.closing = True
        tasks = [task for _, task in self.entries.values()]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.entries.clear()
