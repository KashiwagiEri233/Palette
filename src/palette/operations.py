"""Per-VM operation ownership and runtime admission, independent of transports."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from .errors import ErrorCode, PaletteError
from .policy import validate_identifier


@dataclass
class _Operation:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    owner: asyncio.Task[Any] | None = None
    users: int = 0


class VmOperations:
    """Nested application steps share ownership only within the same asyncio task.

    Admission is recorded before waiting for a VM lock. Runtime exit therefore sees
    queued starts as well as starts that have not yet allocated a process.
    """

    def __init__(self) -> None:
        self._operations: dict[str, _Operation] = {}
        self._starts: dict[asyncio.Task[Any], int] = {}
        self.closing = False

    @property
    def starting(self) -> bool:
        return bool(self._starts)

    def require_accepting(self) -> None:
        if self.closing:
            raise PaletteError(ErrorCode.STATE_CONFLICT, "the runtime is stopping")

    @asynccontextmanager
    async def hold(self, vm_id: str, *, starting: bool = False) -> AsyncIterator[None]:
        validate_identifier(vm_id, what="vm_id")
        task = asyncio.current_task()
        assert task is not None  # noqa: S101
        if starting:
            self.require_accepting()
            self._starts[task] = self._starts.get(task, 0) + 1
        operation = self._operations.setdefault(vm_id, _Operation())
        operation.users += 1
        nested = operation.owner is task
        acquired = False
        try:
            if not nested:
                await operation.lock.acquire()
                acquired = True
                operation.owner = task
            if starting:
                self.require_accepting()
            yield
        finally:
            if acquired:
                operation.owner = None
                operation.lock.release()
            operation.users -= 1
            if operation.users == 0:
                self._operations.pop(vm_id, None)
            if starting:
                remaining = self._starts[task] - 1
                if remaining:
                    self._starts[task] = remaining
                else:
                    self._starts.pop(task)

    async def seal(self) -> None:
        """Close admission and join cancelled starts, including their owned rollback."""
        self.closing = True
        current = asyncio.current_task()
        tasks = [task for task in self._starts if task is not current]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
