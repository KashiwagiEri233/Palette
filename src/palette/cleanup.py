"""Join resource cleanup before propagating cancellation to its caller."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable

logger = logging.getLogger("palette.cleanup")


async def join_cleanup[T](task: asyncio.Task[T]) -> T:
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


async def close_all(steps: Iterable[Callable[[], Awaitable[object]]]) -> None:
    """Try every independent cleanup step and preserve the first failure."""
    failure: BaseException | None = None
    for close in steps:
        try:
            await close()
        except BaseException as exc:
            if failure is None:
                failure = exc
            else:
                logger.error("additional cleanup failure: %s", type(exc).__name__)
    if failure is not None:
        raise failure
