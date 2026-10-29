"""Private management sockets retain subprocess ownership and cancellation cleanup."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from palette import windows_unix
from palette.errors import PaletteError, QmpDisconnectedError
from palette.qmp.transport import DEFAULT_STREAM_LIMIT, MemoryWriter, connect_process_qmp


async def test_connected_socket_uses_large_qmp_frame_buffer(monkeypatch):
    reader, writer = asyncio.StreamReader(), MemoryWriter(lambda _: None)
    connect = AsyncMock(return_value=(reader, writer))
    monkeypatch.setattr(windows_unix, "open_connection", connect)
    process = SimpleNamespace(wait=AsyncMock(side_effect=asyncio.Event().wait))
    transport = await connect_process_qmp(process, "qmp.sock", timeout=1)
    assert transport.kind == "unix" and transport.process is process
    connect.assert_awaited_once_with("qmp.sock", limit=DEFAULT_STREAM_LIMIT)
    await transport.close()
    assert writer._closed


async def test_exit_before_socket_attach_does_not_wait_for_deadline(monkeypatch):
    cancelled = asyncio.Event()

    async def connect(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(windows_unix, "open_connection", connect)
    process = SimpleNamespace(wait=AsyncMock(return_value=7))
    with pytest.raises(QmpDisconnectedError, match="exited before QMP"):
        await asyncio.wait_for(connect_process_qmp(process, "qmp.sock", timeout=30), 0.5)
    assert cancelled.is_set()


async def test_connection_timeout_cancels_both_owned_waiters(monkeypatch):
    async def pending(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(windows_unix, "open_connection", pending)
    process = SimpleNamespace(wait=pending)
    with pytest.raises(PaletteError, match="socket connection timed out"):
        await connect_process_qmp(process, "qmp.sock", timeout=0.01)


async def test_cancel_at_completed_socket_handoff_still_closes_writer(monkeypatch):
    reader, writer = asyncio.StreamReader(), MemoryWriter(lambda _: None)
    monkeypatch.setattr(windows_unix, "open_connection", AsyncMock(return_value=(reader, writer)))
    entered = asyncio.Event()

    async def wait(*args, **kwargs):
        await asyncio.sleep(0)
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(asyncio, "wait", wait)
    process = SimpleNamespace(wait=AsyncMock(side_effect=asyncio.Event().wait))
    pending = asyncio.create_task(connect_process_qmp(process, "qmp.sock", timeout=30))
    await entered.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert writer._closed
