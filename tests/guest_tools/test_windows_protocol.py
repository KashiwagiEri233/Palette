"""The new worker categories negotiate, cancel and reconnect without replay."""

from unittest.mock import AsyncMock

import pytest

from palette.config import GuestToolsConfig
from palette.errors import ErrorCode, PaletteError
from palette.guest_tools.bridge import (
    BridgeGuestToolBackend,
    MemoryGuestToolTransport,
    VirtioSerialGuestToolTransport,
)


def peer(*, version="1.1", stall=False, reconnect=False):
    def respond(message):
        if message["kind"] == "hello":
            assert message["v"] == "1.0"
            return {
                "v": version,
                "kind": "hello_ack",
                "id": message["id"],
                "payload": {
                    "version": version,
                    "capabilities": ["guest.filesystem", "guest.clipboard", "guest.input_text"],
                    "input_methods": ["native_text"],
                    "session": "interactive",
                    "interactive_desktop": True,
                },
            }
        if message["kind"] != "request" or stall:
            return None
        return {
            "v": version,
            "kind": "response",
            "id": message["id"],
            "payload": {
                "ok": not reconnect,
                "effect_state": "not_sent" if reconnect else "submitted",
                "data": {"reconnect_required": True} if reconnect else {"content": "中文 😀"},
            },
        }

    return respond


def bridge(transport, timeout=1):
    return BridgeGuestToolBackend(
        transport=transport,
        config=GuestToolsConfig(request_timeout_seconds=timeout),
        vm_id="desktop",
        instance_id="i",
        boot_epoch=1,
    )


async def test_operation_data_and_clipboard_mode_cross_the_wire():
    transport = MemoryGuestToolTransport(peer())
    backend = bridge(transport)
    try:
        await backend.connect()
        result = await backend.execute_operation("filesystem", {"mode": "read", "path": "file"})
        assert result.data["content"] == "中文 😀"
        await backend.execute_operation("clipboard.set", {"text": "hello"})
        payload = transport.sent[-1]["payload"]
        assert payload["capability"] == "guest.clipboard"
        assert payload["params"] == {"operation": "set", "text": "hello"}
        assert payload["connect_id"] == transport.sent[0]["payload"]["connect_id"]
        assert payload["timeout_ms"] == 1000
    finally:
        await backend.close()


async def test_legacy_negotiation_never_grants_new_categories():
    backend = bridge(MemoryGuestToolTransport(peer(version="1.0")))
    try:
        caps = await backend.connect()
        assert caps.capabilities == ("guest.input_text",)
        with pytest.raises(PaletteError) as caught:
            await backend.execute_operation("filesystem", {"mode": "list", "path": "file"})
        assert caught.value.code == ErrorCode.UNSUPPORTED_CAPABILITY
    finally:
        await backend.close()


async def test_timeout_sends_cancel_and_requires_handshake_without_replay():
    transport = MemoryGuestToolTransport(peer(stall=True))
    backend = bridge(transport, 0.05)
    try:
        await backend.connect()
        with pytest.raises(PaletteError) as caught:
            await backend.execute_operation(
                "filesystem", {"mode": "write", "path": "file", "content": "x"}
            )
        assert caught.value.code == ErrorCode.ACTION_OUTCOME_UNKNOWN
        assert [message["kind"] for message in transport.sent] == ["hello", "request", "cancel"]
        assert transport.sent[-1]["id"] == transport.sent[-2]["id"]
        assert not (await backend.capabilities()).usable
        transport._responder = peer()
        assert (await backend.connect()).usable
        assert len([item for item in transport.sent if item["kind"] == "request"]) == 1
    finally:
        await backend.close()


async def test_restarted_service_reply_invalidates_readiness():
    backend = bridge(MemoryGuestToolTransport(peer(reconnect=True)))
    try:
        await backend.connect()
        result = await backend.execute_operation("filesystem", {"mode": "info", "path": "file"})
        assert not result.ok and result.effect_state == "not_sent"
        assert not (await backend.capabilities()).usable
    finally:
        await backend.close()


async def test_private_transport_keeps_idle_pipe_and_marks_disconnect(monkeypatch):
    channel = AsyncMock()
    channel.connected = True
    monkeypatch.setattr("palette.guest_tools.bridge.GuestChannel", lambda endpoint: channel)
    transport = VirtioSerialGuestToolTransport(endpoint="private-instance-pipe")
    await transport.open()
    assert transport.online
    channel.read.side_effect = TimeoutError
    assert await transport.receive(0.01) == b""
    assert transport.online
    await transport.send(b"framed-data")
    channel.write.assert_awaited_once_with(b"framed-data")
    channel.read.side_effect = EOFError
    with pytest.raises(EOFError):
        await transport.receive(0.01)
    assert not transport.online
    await transport.close()
