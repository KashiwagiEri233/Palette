"""Operation ownership, request replay and cleanup under cancellation."""

from __future__ import annotations

import asyncio

import pytest

from palette.cleanup import join_cleanup
from palette.errors import ErrorCode, PaletteError
from palette.operations import VmOperations
from palette.operator.requests import RequestRegistry


async def test_same_vm_serializes_nested_steps_but_other_vms_progress() -> None:
    operations = VmOperations()
    entered = asyncio.Event()

    async def competing(vm_id: str) -> None:
        async with operations.hold(vm_id):
            entered.set()

    async with operations.hold("first"), operations.hold("first"):
        waiting = asyncio.create_task(competing("first"))
        await asyncio.wait_for(competing("second"), 1)
        entered.clear()
        await asyncio.sleep(0)
        assert not entered.is_set()
    await asyncio.wait_for(waiting, 1)
    assert entered.is_set()


async def test_shutdown_cancels_a_start_waiting_for_its_vm_lock() -> None:
    operations = VmOperations()

    async def start() -> None:
        async with operations.hold("desktop", starting=True):
            pytest.fail("a queued start entered after shutdown")

    async with operations.hold("desktop"):
        task = asyncio.create_task(start())
        await asyncio.sleep(0)
        assert operations.starting
        await operations.seal()
    assert task.cancelled()
    assert not operations.starting
    with pytest.raises(PaletteError) as error:
        async with operations.hold("other", starting=True):
            pass
    assert error.value.code is ErrorCode.STATE_CONFLICT


async def test_repeated_cancellation_joins_cleanup_before_propagating() -> None:
    entered = asyncio.Event()
    finish = asyncio.Event()
    released = asyncio.Event()

    async def cleanup() -> None:
        entered.set()
        await finish.wait()
        released.set()

    owned = asyncio.create_task(cleanup())
    caller = asyncio.create_task(join_cleanup(owned))
    await entered.wait()
    caller.cancel()
    await asyncio.sleep(0)
    caller.cancel()
    await asyncio.sleep(0)
    assert not caller.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert released.is_set()


async def test_disconnected_waiter_does_not_replay_or_cancel_owned_request() -> None:
    registry = RequestRegistry[int](pending_limit=1, retained_limit=2)
    entered = asyncio.Event()
    finish = asyncio.Event()
    calls = 0

    async def operation() -> int:
        nonlocal calls
        calls += 1
        entered.set()
        await finish.wait()
        return 42

    first = asyncio.create_task(registry.execute("request", "same", operation))
    await entered.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    with pytest.raises(PaletteError) as conflict:
        await registry.execute("request", "different", operation)
    assert conflict.value.code is ErrorCode.REQUEST_ID_CONFLICT
    with pytest.raises(PaletteError) as busy:
        await registry.execute("another", "same", operation)
    assert busy.value.code is ErrorCode.BUSY
    finish.set()
    assert await registry.execute("request", "same", operation) == 42
    assert calls == 1
    await registry.close()
