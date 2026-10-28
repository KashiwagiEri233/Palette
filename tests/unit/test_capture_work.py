"""CPU image work must yield the shared loop and be joined before cancellation."""

from __future__ import annotations

import asyncio
import threading

import pytest

from palette.capture.work import image_work
from palette.timing import TimingWindow


async def test_image_worker_yields_loop_and_joins_cancellation() -> None:
    entered = asyncio.Event()
    loop = asyncio.get_running_loop()
    gate = threading.Event()
    completed = threading.Event()

    def work() -> None:
        loop.call_soon_threadsafe(entered.set)
        gate.wait(5)
        completed.set()

    task = asyncio.create_task(image_work(work))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
        assert not completed.is_set()
        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert completed.is_set()
    finally:
        gate.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_only_two_image_workers_execute_at_once() -> None:
    gate = threading.Event()
    lock = threading.Lock()
    active = 0
    peak = 0
    entered = asyncio.Event()
    loop = asyncio.get_running_loop()

    def work() -> None:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if peak == 2:
                loop.call_soon_threadsafe(entered.set)
        gate.wait(5)
        with lock:
            active -= 1

    tasks = [asyncio.create_task(image_work(work)) for _ in range(5)]
    try:
        await asyncio.wait_for(entered.wait(), 3)
        await asyncio.sleep(0.01)
        assert active == 2
    finally:
        gate.set()
        await asyncio.gather(*tasks)
    assert peak == 2


def test_timing_windows_are_bounded_and_ignore_nonfinite_values() -> None:
    timings = TimingWindow()
    for value in range(300):
        timings.record(queue_ms=float(value))
    timings.record(queue_ms=float("nan"))
    assert timings.snapshot()["queue_ms"] == {
        "count": 256,
        "p50": 171.0,
        "p95": 287.0,
        "p99": 297.0,
    }
