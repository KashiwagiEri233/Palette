"""Native adapter control-flow tests; real pipe coverage runs on Windows integration CI."""

# ruff: noqa: N802 - the fake implements the Win32 API names used by ctypes.

from __future__ import annotations

import asyncio
import ctypes
import threading

import pytest

from palette.operator import windows


class FakeKernel:
    sid = "S-1-5-21-123"

    def __init__(self, *, stall: bool = False) -> None:
        self.calls: list[tuple] = []
        self.counter = 100
        self.stall = stall
        self.waiting = threading.Event()
        self.stopped = threading.Event()

    def CreateEvent(self, *args):
        self.counter += 1
        return self.counter

    def ConvertSecurityDescriptor(self, text, revision, output, size):
        self.calls.append(("security", text))
        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = 123
        return True

    def LocalFree(self, pointer):
        self.calls.append(("free",))

    def CreateNamedPipe(self, name, flags, mode, maximum, outbound, inbound, timeout, security):
        attributes = ctypes.cast(security, ctypes.POINTER(windows._SecurityAttributes)).contents
        self.calls.append(("pipe", flags, mode, bool(attributes.bInheritHandle)))
        return 55

    def ReadFile(self, *args):
        return False

    def WaitForMultipleObjects(self, count, handles, wait_all, timeout):
        self.waiting.set()
        if self.stall:
            assert self.stopped.wait(2)
            return 1
        return 258

    def SetEvent(self, handle):
        self.stopped.set()
        return True

    def CancelIoEx(self, handle, overlapped):
        self.calls.append(("cancel", handle))
        return True

    def GetOverlappedResult(self, handle, overlapped, count, wait):
        self.calls.append(("completion", bool(wait)))
        return False

    def CloseHandle(self, handle):
        self.calls.append(("close", handle))
        return True


class CapacityKernel(FakeKernel):
    def __init__(self) -> None:
        super().__init__()
        self.pipes: set[int] = set()
        self.connected: set[int] = set()
        self.capacity_failures = 0
        self.rejected = asyncio.Event()
        self.error = 0

    def CreateNamedPipe(self, name, flags, mode, maximum, outbound, inbound, timeout, security):
        super().CreateNamedPipe(name, flags, mode, maximum, outbound, inbound, timeout, security)
        if len(self.pipes) >= maximum:
            self.capacity_failures += 1
            self.error = 231
            return windows._INVALID_HANDLE
        self.counter += 1
        self.pipes.add(self.counter)
        return self.counter

    def ConnectNamedPipe(self, handle, overlapped):
        assert handle in self.pipes and handle not in self.connected
        self.connected.add(handle)
        return True

    def GetOverlappedResult(self, handle, overlapped, count, wait):
        return True

    def DisconnectNamedPipe(self, handle):
        self.connected.remove(handle)
        self.rejected.set()
        return True

    def CloseHandle(self, handle):
        self.pipes.discard(handle)
        self.connected.discard(handle)
        return super().CloseHandle(handle)


def capacity_kernel(monkeypatch):
    api = CapacityKernel()
    monkeypatch.setattr(windows, "_api", lambda: api)
    monkeypatch.setattr(windows, "_get_last_error", lambda: api.error)
    monkeypatch.setattr(windows, "_WinError", lambda code: OSError(code, "fake Windows error"))
    return api


async def test_capacity_exhaustion_reuses_listener_and_recovers_when_a_slot_frees(monkeypatch):
    api = capacity_kernel(monkeypatch)
    listener = windows.WindowsListener(r"\\.\pipe\palette-capacity")
    clients = []
    pending = None
    try:
        for _ in range(9):
            clients.append(await listener.accept())
        listening_handle = listener._next.handle
        pending = asyncio.create_task(listener.accept())
        await asyncio.wait_for(api.rejected.wait(), 2)
        assert not pending.done()
        assert api.capacity_failures == 1
        assert len(api.pipes) == 10
        assert listening_handle in api.pipes
        assert listening_handle not in api.connected

        await clients.pop().close()
        accepted = await asyncio.wait_for(pending, 2)
        clients.append(accepted)
        assert accepted.handle == listening_handle
        assert listener._next.handle != listening_handle
        assert len(api.pipes) == 10

        await clients.pop().close()
        clients.append(await asyncio.wait_for(listener.accept(), 2))
    finally:
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await listener.close()
        await asyncio.gather(*(channel.close() for channel in clients))
    assert not api.pipes


@pytest.mark.parametrize("stop", ["close", "cancel"])
async def test_capacity_backoff_stops_promptly_without_allocating_more_pipes(monkeypatch, stop):
    api = capacity_kernel(monkeypatch)
    monkeypatch.setattr(windows, "_ACCEPT_RETRY_SECONDS", 60)
    listener = windows.WindowsListener(r"\\.\pipe\palette-capacity-stop")
    clients = []
    pending = None
    try:
        for _ in range(9):
            clients.append(await listener.accept())
        pending = asyncio.create_task(listener.accept())
        await asyncio.wait_for(api.rejected.wait(), 2)
        await asyncio.sleep(0)
        assert api.capacity_failures == 1
        if stop == "close":
            await listener.close()
            with pytest.raises(OSError, match="listener is closed"):
                await asyncio.wait_for(pending, 1)
        else:
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(pending, 1)
        assert api.capacity_failures == 1
    finally:
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await listener.close()
        await asyncio.gather(*(channel.close() for channel in clients))
    assert not api.pipes


async def test_pipe_is_local_private_overlapped_and_exclusive(monkeypatch) -> None:
    api = FakeKernel()
    monkeypatch.setattr(windows, "_api", lambda: api)
    listener = windows.WindowsListener(r"\\.\pipe\palette-test")
    try:
        assert ("security", "D:P(A;;GA;;;S-1-5-21-123)") in api.calls
        pipe = next(call for call in api.calls if call[0] == "pipe")
        assert pipe[1] & 0x40000000  # FILE_FLAG_OVERLAPPED
        assert pipe[1] & 0x00080000  # FILE_FLAG_FIRST_PIPE_INSTANCE
        assert pipe[2] & 0x00000008  # PIPE_REJECT_REMOTE_CLIENTS
        assert pipe[3] is False
    finally:
        await listener.close()


async def test_io_timeout_cancels_and_reaps_before_releasing_overlapped_event(monkeypatch) -> None:
    api = FakeKernel()
    monkeypatch.setattr(windows, "_api", lambda: api)
    monkeypatch.setattr(windows, "_get_last_error", lambda: 997)
    channel = windows.WindowsChannel(55)
    try:
        with pytest.raises(TimeoutError):
            await channel.readexactly(8)
        cancel = api.calls.index(("cancel", 55))
        completion = api.calls.index(("completion", True))
        event_close = next(index for index, call in enumerate(api.calls) if call[0] == "close")
        assert cancel < completion < event_close
    finally:
        await channel.close()


async def test_asyncio_cancellation_waits_for_the_native_worker(monkeypatch) -> None:
    api = FakeKernel(stall=True)
    monkeypatch.setattr(windows, "_api", lambda: api)
    monkeypatch.setattr(windows, "_get_last_error", lambda: 997)
    channel = windows.WindowsChannel(55)
    task = asyncio.create_task(channel.readexactly(8))
    assert await asyncio.to_thread(api.waiting.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await channel.close()
    assert api.calls.index(("completion", True)) < api.calls.index(("close", 55))
    assert not channel._workers
