"""Bound image work without blocking the shared control/display event loop."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from weakref import ReferenceType, WeakKeyDictionary, ref

from ..cleanup import join_cleanup

_limits: WeakKeyDictionary[asyncio.AbstractEventLoop, ReferenceType[asyncio.Semaphore]] = (
    WeakKeyDictionary()
)


async def image_work[T](work: Callable[[], T]) -> T:
    loop = asyncio.get_running_loop()
    previous = _limits.get(loop)
    limit = previous() if previous is not None else None
    if limit is None:
        limit = asyncio.Semaphore(2)
        # A contended Semaphore retains its loop. Keep both sides weak so closed
        # test/runtime loops are not kept alive by this module-level registry.
        _limits[loop] = ref(limit)
    async with limit:
        # A cancelled coroutine must not release its slot or delete a sink while
        # its worker still uses it. State publication remains on the event loop.
        return await join_cleanup(asyncio.create_task(asyncio.to_thread(work)))
