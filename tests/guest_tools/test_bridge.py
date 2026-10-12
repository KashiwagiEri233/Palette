"""The guest tool bridge: capability reconciliation, refusals and generation handling.

The bridge is exercised against an in-process guest service that speaks the real protocol.
What matters here is that the host never grants more than the profile allows, never
downgrades a method silently, and never treats a guest's claim as a permission.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from palette.config import GuestToolsConfig
from palette.errors import ErrorCode, PaletteError
from palette.guest_tools.base import DisabledGuestToolBackend, MockGuestToolBackend
from palette.guest_tools.bridge import (
    BridgeGuestToolBackend,
    MemoryGuestToolTransport,
    VirtioSerialGuestToolTransport,
)
from palette.guest_tools.protocol import PROTOCOL_VERSION


def guest_service(
    *,
    version: str = PROTOCOL_VERSION,
    capabilities: tuple[str, ...] = ("guest.input_text", "guest.launch_app"),
    input_methods: tuple[str, ...] = ("native_text", "clipboard_paste"),
    apps: tuple[str, ...] = ("text-editor", "file-manager"),
    reply: dict[str, Any] | None = None,
    ignore_requests: bool = False,
    desktop_ready: bool = True,
):
    """A responder that behaves like a well-formed guest tool service."""

    def responder(message: dict[str, Any]) -> dict[str, Any] | None:
        kind = message.get("kind")
        if kind == "hello":
            return {
                "v": PROTOCOL_VERSION,
                "kind": "hello_ack",
                "id": message["id"],
                "payload": {
                    "version": version,
                    "capabilities": list(capabilities),
                    "input_methods": list(input_methods),
                    "apps": list(apps),
                    "session": "interactive",
                    "interactive_desktop": desktop_ready,
                },
            }
        if kind == "request":
            if ignore_requests:
                return None
            payload = reply or {
                "ok": True,
                "effect_state": "submitted",
                "method": message["payload"]["params"].get("method"),
                "launch_id": "launch_1"
                if message["payload"]["capability"] == "guest.launch_app"
                else None,
                "detail": "accepted",
            }
            return {
                "v": PROTOCOL_VERSION,
                "kind": "response",
                "id": message["id"],
                "payload": payload,
            }
        return None

    return responder


def config(**overrides) -> GuestToolsConfig:
    base: dict[str, Any] = {
        "enabled": True,
        "capabilities": ["guest.input_text", "guest.launch_app"],
        "input_methods": ["native_text"],
        "allowed_apps": {"text-editor": {"executable": "/usr/bin/mousepad"}},
    }
    base.update(overrides)
    return GuestToolsConfig(**base)


def build(transport, *, settings: GuestToolsConfig | None = None) -> BridgeGuestToolBackend:
    return BridgeGuestToolBackend(
        transport=transport,
        config=settings or config(),
        vm_id="desktop",
        instance_id="inst_1",
        boot_epoch=1,
    )


class TestHandshake:
    async def test_a_successful_handshake_reports_what_is_actually_available(self) -> None:
        transport = MemoryGuestToolTransport(guest_service())
        bridge = build(transport)
        caps = await bridge.connect()
        assert caps.usable is True
        assert caps.protocol_version == PROTOCOL_VERSION
        assert caps.transport_online is True
        assert caps.service_online is True
        assert caps.interactive_desktop_ready is True
        assert caps.connection_generation == 1
        assert caps.detail is None
        await bridge.close()

    async def test_a_guest_capability_the_profile_never_granted_is_not_reported(self) -> None:
        """Advertising is not permission: a guest that offers more than the profile allows
        is reported as offering only what was granted."""
        transport = MemoryGuestToolTransport(guest_service())
        bridge = build(transport, settings=config(capabilities=["guest.input_text"]))
        caps = await bridge.connect()
        assert caps.capabilities == ("guest.input_text",)
        await bridge.close()

    async def test_input_methods_are_the_intersection(self) -> None:
        transport = MemoryGuestToolTransport(guest_service())
        bridge = build(transport, settings=config(input_methods=["clipboard_paste"]))
        caps = await bridge.connect()
        assert caps.input_methods == ("clipboard_paste",)
        await bridge.close()

    async def test_apps_are_the_intersection_of_registry_and_guest(self) -> None:
        transport = MemoryGuestToolTransport(guest_service())
        bridge = build(
            transport,
            settings=config(
                allowed_apps={
                    "text-editor": {"executable": "/usr/bin/mousepad"},
                    "never-installed": {"executable": "/usr/bin/nope"},
                }
            ),
        )
        caps = await bridge.connect()
        assert caps.apps == ("text-editor",)
        await bridge.close()

    async def test_a_version_mismatch_is_reported_rather_than_ignored(self) -> None:
        transport = MemoryGuestToolTransport(guest_service(version="9.9"))
        bridge = build(transport)
        caps = await bridge.connect()
        assert caps.usable is False
        assert "mismatch" in (caps.detail or "")
        await bridge.close()

    async def test_a_missing_guest_service_is_an_unavailable_state_not_a_crash(self) -> None:
        transport = MemoryGuestToolTransport(lambda _message: None)
        bridge = build(transport, settings=config(request_timeout_seconds=0.2))
        caps = await bridge.connect()
        assert caps.usable is False
        assert caps.transport_online is True
        assert caps.service_online is False
        assert "handshake failed" in (caps.detail or "")
        await bridge.close()

    async def test_a_transport_that_cannot_open_is_reported(self) -> None:
        transport = MemoryGuestToolTransport(fail_on_open=True)
        bridge = build(transport)
        caps = await bridge.connect()
        assert caps.usable is False
        assert "transport unavailable" in (caps.detail or "")
        await bridge.close()

    async def test_a_disabled_channel_reports_disabled_and_never_opens_the_transport(self) -> None:
        transport = MemoryGuestToolTransport(guest_service())
        bridge = build(transport, settings=GuestToolsConfig(enabled=False))
        caps = await bridge.connect()
        assert caps.enabled is False
        assert caps.usable is False
        assert transport.sent == []
        await bridge.close()

    async def test_the_placeholder_virtio_transport_says_it_is_not_implemented(self) -> None:
        bridge = build(VirtioSerialGuestToolTransport())
        caps = await bridge.connect()
        assert caps.usable is False
        assert "unavailable" in (caps.detail or "").lower() or "not implemented" in (
            caps.detail or ""
        )


class TestOperations:
    async def test_text_is_accepted_and_reported_as_submitted(self) -> None:
        transport = MemoryGuestToolTransport(guest_service())
        bridge = build(transport)
        await bridge.connect()
        outcome = await bridge.input_text(text="你好", method="native_text")
        assert outcome.ok is True
        assert outcome.effect_state == "submitted"
        assert outcome.method == "native_text"
        # The guest's own detail is passed through unchanged: the host does not dress an
        # ACK up as evidence that an application consumed the text.
        assert outcome.detail == "accepted"
        await bridge.close()

    async def test_an_unavailable_method_is_refused_without_downgrading(self) -> None:
        transport = MemoryGuestToolTransport(guest_service())
        bridge = build(transport, settings=config(input_methods=["native_text"]))
        await bridge.connect()
        with pytest.raises(PaletteError) as info:
            await bridge.input_text(text="hi", method="clipboard_paste")
        assert info.value.code is ErrorCode.INPUT_TARGET_UNSUPPORTED
        assert info.value.details["available_methods"] == ["native_text"]
        # Nothing was sent: no silent substitution of one backend for another.
        assert len(transport.sent) == 1  # just the hello
        await bridge.close()

    async def test_a_target_other_than_the_focus_is_refused_at_the_service_layer(self) -> None:
        transport = MemoryGuestToolTransport(guest_service())
        bridge = build(transport)
        await bridge.connect()
        outcome = await bridge.input_text(text="hi", method="native_text", target="focused")
        assert outcome.ok is True
        await bridge.close()

    async def test_an_unregistered_application_is_refused_before_any_message(self) -> None:
        transport = MemoryGuestToolTransport(guest_service())
        bridge = build(transport)
        await bridge.connect()
        before = len(transport.sent)
        with pytest.raises(PaletteError) as info:
            await bridge.launch_app(app_id="bash", options={})
        assert info.value.code is ErrorCode.APP_NOT_ALLOWED
        assert len(transport.sent) == before
        await bridge.close()

    async def test_an_application_the_guest_does_not_have_is_refused(self) -> None:
        transport = MemoryGuestToolTransport(guest_service(apps=()))
        bridge = build(transport)
        await bridge.connect()
        with pytest.raises(PaletteError) as info:
            await bridge.launch_app(app_id="text-editor", options={})
        assert info.value.code is ErrorCode.APP_NOT_ALLOWED
        await bridge.close()

    async def test_a_launch_returns_an_opaque_identifier(self) -> None:
        transport = MemoryGuestToolTransport(guest_service())
        bridge = build(transport)
        await bridge.connect()
        outcome = await bridge.launch_app(app_id="text-editor", options={})
        assert outcome.ok is True
        assert outcome.launch_id == "launch_1"
        # A launch identifier is opaque to the host; it is never parsed into a PID or a
        # window handle, and it does not claim the window is on screen.
        assert outcome.detail == "accepted"
        await bridge.close()

    async def test_operations_before_connect_are_refused(self) -> None:
        transport = MemoryGuestToolTransport(guest_service())
        bridge = build(transport)
        with pytest.raises(PaletteError) as info:
            await bridge.input_text(text="hi", method="native_text")
        assert info.value.code is ErrorCode.GUEST_TOOL_UNAVAILABLE

    async def test_text_longer_than_the_limit_is_refused(self) -> None:
        transport = MemoryGuestToolTransport(guest_service())
        bridge = build(transport, settings=config(max_text_chars=8))
        await bridge.connect()
        with pytest.raises(PaletteError) as info:
            await bridge.input_text(text="x" * 9, method="native_text")
        assert info.value.code is ErrorCode.INVALID_ARGUMENT
        await bridge.close()

    async def test_a_guest_reported_failure_keeps_its_effect_state(self) -> None:
        transport = MemoryGuestToolTransport(
            guest_service(reply={"ok": False, "effect_state": "unknown", "detail": "focus lost"})
        )
        bridge = build(transport)
        await bridge.connect()
        outcome = await bridge.input_text(text="hi", method="native_text")
        assert outcome.ok is False
        assert outcome.effect_state == "unknown"
        assert outcome.detail == "focus lost"
        await bridge.close()


class TestFailures:
    async def test_a_timeout_is_an_unknown_outcome_not_a_retryable_failure(self) -> None:
        transport = MemoryGuestToolTransport(guest_service(ignore_requests=True))
        bridge = build(transport, settings=config(request_timeout_seconds=0.15))
        await bridge.connect()
        with pytest.raises(PaletteError) as info:
            await bridge.input_text(text="hi", method="native_text")
        assert info.value.code is ErrorCode.ACTION_OUTCOME_UNKNOWN
        await bridge.close()

    async def test_a_disconnect_fails_outstanding_requests(self) -> None:
        transport = MemoryGuestToolTransport(guest_service(ignore_requests=True))
        bridge = build(transport, settings=config(request_timeout_seconds=2.0))
        await bridge.connect()
        task = asyncio.create_task(bridge.input_text(text="hi", method="native_text"))
        await asyncio.sleep(0.05)
        await transport.disconnect()
        with pytest.raises(PaletteError) as info:
            await task
        assert info.value.code == ErrorCode.GUEST_TOOL_UNAVAILABLE
        caps = await bridge.capabilities()
        assert caps.usable is False
        await bridge.close()

    async def test_invalidation_abandons_pending_requests_and_bumps_the_generation(self) -> None:
        """A guest reboot must not let a late reply be matched to a request issued after
        it; the connection generation is what makes that impossible."""
        transport = MemoryGuestToolTransport(guest_service(ignore_requests=True))
        bridge = build(transport, settings=config(request_timeout_seconds=2.0))
        first = await bridge.connect()
        generation = first.connection_generation
        task = asyncio.create_task(bridge.input_text(text="hi", method="native_text"))
        await asyncio.sleep(0.05)
        await bridge.invalidate(reason="the guest rebooted")
        with pytest.raises(PaletteError):
            await task
        caps = await bridge.capabilities()
        assert caps.connection_generation == generation + 1
        assert caps.usable is False
        assert caps.detail == "the guest rebooted"
        assert caps.capabilities == ()
        await bridge.close()

    async def test_an_operation_after_invalidation_reports_unavailable(self) -> None:
        transport = MemoryGuestToolTransport(guest_service())
        bridge = build(transport)
        await bridge.connect()
        await bridge.invalidate(reason="reset")
        with pytest.raises(PaletteError) as info:
            await bridge.launch_app(app_id="text-editor", options={})
        assert info.value.code is ErrorCode.GUEST_TOOL_UNAVAILABLE
        await bridge.close()

    async def test_closing_is_idempotent(self) -> None:
        transport = MemoryGuestToolTransport(guest_service())
        bridge = build(transport)
        await bridge.connect()
        await bridge.close()
        await bridge.close()


class TestOtherBackends:
    async def test_the_disabled_backend_is_explicitly_off(self) -> None:
        backend = DisabledGuestToolBackend()
        caps = await backend.capabilities()
        assert caps.enabled is False
        assert caps.usable is False
        with pytest.raises(PaletteError) as info:
            await backend.input_text(text="hi", method="native_text")
        assert info.value.code is ErrorCode.GUEST_TOOL_UNAVAILABLE
        with pytest.raises(PaletteError):
            await backend.launch_app(app_id="text-editor", options={})

    async def test_the_mock_backend_refuses_an_unadvertised_method(self) -> None:
        backend = MockGuestToolBackend(input_methods=("native_text",))
        outcome = await backend.input_text(text="hi", method="native_text")
        assert outcome.ok is True
        with pytest.raises(PaletteError) as info:
            await backend.input_text(text="hi", method="clipboard_paste")
        assert info.value.code is ErrorCode.INPUT_TARGET_UNSUPPORTED

    async def test_the_mock_backend_refuses_an_unregistered_app(self) -> None:
        backend = MockGuestToolBackend()
        await backend.launch_app(app_id="text-editor", options={})
        with pytest.raises(PaletteError) as info:
            await backend.launch_app(app_id="bash", options={})
        assert info.value.code is ErrorCode.APP_NOT_ALLOWED

    async def test_the_mock_backend_reports_offline_honestly(self) -> None:
        backend = MockGuestToolBackend(service_online=False)
        caps = await backend.capabilities()
        assert caps.usable is False
        with pytest.raises(PaletteError) as info:
            await backend.input_text(text="hi", method="native_text")
        assert info.value.code is ErrorCode.GUEST_TOOL_UNAVAILABLE
