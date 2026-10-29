"""Bounded socket attachment, cancellation and optional-service independence."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from palette import guest_channel, windows_unix
from palette.guest_channel import GuestChannel


@pytest.fixture(autouse=True)
def windows_guest_channel(monkeypatch):
    monkeypatch.setattr(guest_channel, "sys", SimpleNamespace(platform="win32"))


def streams():
    reader = asyncio.StreamReader()
    writer = Mock()
    writer.is_closing.return_value = False
    writer.wait_closed = AsyncMock()
    writer.drain = AsyncMock()
    return reader, writer


async def test_connected_socket_is_reused(monkeypatch):
    reader, writer = streams()
    connect = AsyncMock(return_value=(reader, writer))
    monkeypatch.setattr(windows_unix, "open_connection", connect)
    channel = GuestChannel("tools.sock")
    await channel.open()
    await channel.open()
    assert channel.connected
    assert connect.await_count == 1
    await channel.close()
    writer.close.assert_called_once()


async def test_failed_reader_is_not_reused_on_reconnect(monkeypatch):
    first_reader, first_writer = streams()
    second_reader, second_writer = streams()
    connect = AsyncMock(side_effect=[(first_reader, first_writer), (second_reader, second_writer)])
    monkeypatch.setattr(windows_unix, "open_connection", connect)
    channel = GuestChannel("tools.sock")
    await channel.open()
    first_reader.set_exception(ConnectionResetError("peer reset"))
    assert not channel.connected
    await channel.open()
    first_writer.close.assert_called_once()
    second_reader.feed_data(b"new reply")
    assert await channel.read() == b"new reply"
    await channel.close()


async def test_connectors_do_not_wait_for_other_guest_service_handshakes(monkeypatch):
    pairs = {"tools.sock": streams(), "qga.sock": streams()}
    connect = AsyncMock(side_effect=lambda endpoint: pairs[endpoint])
    monkeypatch.setattr(windows_unix, "open_connection", connect)
    tools, qga = GuestChannel("tools.sock"), GuestChannel("qga.sock")
    await asyncio.wait_for(asyncio.gather(tools.open(), qga.open()), 0.5)
    assert tools.connected and qga.connected
    await asyncio.gather(tools.close(), qga.close())


async def test_missing_socket_has_a_bounded_connection_deadline(monkeypatch):
    async def connect(endpoint):
        await asyncio.Event().wait()

    monkeypatch.setattr(windows_unix, "open_connection", connect)
    channel = GuestChannel("missing.sock")
    with pytest.raises(TimeoutError):
        await channel.open(timeout=0.04)
    assert not channel.connected
    assert channel.status()["error"] == "TimeoutError"
    assert channel.status()["state"] == "failed"


async def test_a_socket_created_late_is_still_attached(monkeypatch):
    appeared = asyncio.Event()
    pair = streams()

    async def connect(endpoint):
        await appeared.wait()
        return pair

    monkeypatch.setattr(windows_unix, "open_connection", connect)
    channel = GuestChannel("slow.sock")
    pending = asyncio.create_task(channel.open())
    await asyncio.sleep(0)
    assert channel.status()["state"] == "connecting"
    appeared.set()
    await pending
    assert channel.connected
    await channel.close()


async def test_permission_failures_are_not_retried(monkeypatch):
    connect = AsyncMock(side_effect=PermissionError("denied"))
    monkeypatch.setattr(windows_unix, "open_connection", connect)
    channel = GuestChannel("denied.sock")
    with pytest.raises(PermissionError, match="denied"):
        await channel.open()
    assert connect.await_count == 1
    assert channel.status()["error"] == "PermissionError"
    assert "denied" in channel.status()["error_detail"]


async def test_cancelling_a_connector_leaves_no_connection(monkeypatch):
    attempted = asyncio.Event()

    async def connect(endpoint):
        attempted.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(windows_unix, "open_connection", connect)
    channel = GuestChannel("missing.sock")
    pending = asyncio.create_task(channel.open())
    await attempted.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert not channel.connected
    assert channel.status()["state"] == "cancelled"


async def test_close_during_connection_does_not_publish_late_socket(monkeypatch):
    release = asyncio.Event()
    reader, writer = streams()

    async def connect(endpoint):
        await release.wait()
        return reader, writer

    monkeypatch.setattr(windows_unix, "open_connection", connect)
    channel = GuestChannel("slow.sock")
    pending = asyncio.create_task(channel.open())
    await asyncio.sleep(0)
    await channel.close()
    release.set()
    with pytest.raises(ConnectionError, match="closed during"):
        await pending
    assert not channel.connected
    writer.close.assert_called_once()


async def test_read_timeout_and_cancel_preserve_stream_for_resynchronization(monkeypatch):
    reader, writer = streams()
    monkeypatch.setattr(windows_unix, "open_connection", AsyncMock(return_value=(reader, writer)))
    channel = GuestChannel("tools.sock")
    await channel.open()
    with pytest.raises(TimeoutError):
        await channel.read(timeout=0.01)
    pending = asyncio.create_task(channel.read())
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    reader.feed_data(b"next response")
    assert await channel.read() == b"next response"
    assert channel.connected
    await channel.close()
