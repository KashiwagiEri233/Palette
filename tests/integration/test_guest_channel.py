"""The guest enhancement channel end to end.

A hybrid profile is started with a real QEMU process for the visual side and an in-process
guest service for the enhancement side. That combination is what the plan asks for: the
enhancement path shares the per-VM scheduler, action sequence and audit with visual
actions, and it must never become a substitute for them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from palette.audit import NullAuditLog
from palette.config import GuestToolsConfig, PaletteConfig
from palette.errors import ErrorCode, PaletteError
from palette.guest_tools.bridge import MemoryGuestToolTransport
from palette.service import PaletteService
from support.profiles import make_profile

DEFAULT_SETTINGS = GuestToolsConfig(
    enabled=True,
    capabilities=["guest.input_text", "guest.launch_app"],
    input_methods=["native_text"],
    allowed_apps={"text-editor": {"executable": "/usr/bin/mousepad"}},
)


def guest_service(
    *,
    capabilities: tuple[str, ...] = ("guest.input_text", "guest.launch_app"),
    input_methods: tuple[str, ...] = ("native_text",),
    apps: tuple[str, ...] = ("text-editor",),
    ignore_requests: bool = False,
    reply: dict[str, Any] | None = None,
):
    """A guest tool service that speaks the real protocol."""

    def responder(message: dict[str, Any]) -> dict[str, Any] | None:
        if message.get("kind") == "hello":
            return {
                "v": "1.0",
                "kind": "hello_ack",
                "id": message["id"],
                "payload": {
                    "version": "1.0",
                    "capabilities": list(capabilities),
                    "input_methods": list(input_methods),
                    "apps": list(apps),
                    "session": "interactive",
                    "interactive_desktop": True,
                },
            }
        if message.get("kind") == "request":
            if ignore_requests:
                return None
            return {
                "v": "1.0",
                "kind": "response",
                "id": message["id"],
                "payload": reply
                or {
                    "ok": True,
                    "effect_state": "submitted",
                    "method": message["payload"]["params"].get("method"),
                    "launch_id": "launch_abc",
                    "detail": "accepted by the guest service",
                },
            }
        return None

    return responder


@pytest.fixture
async def hybrid_factory(base_image: Path, runtime_root: Path, tmp_path: Path):
    """Build a started hybrid VM with a chosen guest service behind it."""
    built: list[PaletteService] = []

    async def build(
        *,
        responder=None,
        settings: GuestToolsConfig | None = None,
        timeout_seconds: float = 20.0,
        service_startup: bool = True,
    ):
        effective = (settings or DEFAULT_SETTINGS).model_copy(
            update={"request_timeout_seconds": timeout_seconds}
        )
        profile = make_profile(base_image, guest_tools=effective)
        config = PaletteConfig(profiles={"default": profile}, config_root=tmp_path).model_copy(
            update={
                "host": PaletteConfig().host.model_copy(update={"runtime_root": str(runtime_root)})
            }
        )
        service = PaletteService(
            config,
            audit=NullAuditLog(),
            guest_tool_transport_factory=lambda _instance: MemoryGuestToolTransport(
                responder or guest_service()
            ),
        )
        await service.startup()
        built.append(service)
        instance = await service.manager.start("desktop", "default")
        return service, instance

    yield build
    for service in built:
        await service.shutdown()


@pytest.fixture
async def hybrid(hybrid_factory):
    return await hybrid_factory()


class TestHandshake:
    async def test_a_hybrid_vm_reports_its_guest_capabilities(self, hybrid) -> None:
        service, _instance = hybrid
        status = await service.status("desktop")
        assert status.ok is True
        assert status.status is not None
        guest = status.status.guest_tools
        assert guest.enabled is True
        assert guest.transport_online is True
        assert guest.service_online is True
        assert guest.protocol_version == "1.0"
        assert guest.capabilities == ["guest.input_text", "guest.launch_app"]
        assert guest.input_methods == ["native_text"]
        assert guest.allowed_apps == ["text-editor"]
        # Readiness is still not inferred from the enhancement channel being up.
        assert status.status.desktop_ready is None

    async def test_the_visual_baseline_still_works(self, hybrid) -> None:
        """The plan is explicit that the baseline must stand on its own: nothing about the
        enhancement channel may become a prerequisite for seeing the screen."""
        service, _instance = hybrid
        result, image = await service.observe_image("desktop")
        assert result.ok is True
        assert image.startswith(b"\x89PNG")

    async def test_an_unavailable_guest_service_does_not_block_startup(
        self, hybrid_factory
    ) -> None:
        # Nothing answers at all, not even the handshake: this is the shipped state.
        service, instance = await hybrid_factory(
            responder=lambda _message: None, timeout_seconds=0.2
        )
        assert instance.state.value == "RUNNING"
        status = await service.status("desktop")
        assert status.status is not None
        assert status.status.guest_tools.service_online is False
        assert status.status.guest_tools.detail is not None
        # And the baseline is unaffected.
        assert (await service.observe("desktop")).ok is True


class TestGuestOperations:
    async def test_text_input_is_routed_through_the_shared_scheduler(self, hybrid) -> None:
        service, instance = hybrid
        observed = await service.observe("desktop")
        assert observed.frame is not None
        result, image = await service.guest_input_text(
            "desktop",
            instance_id=instance.view.instance_id,
            boot_epoch=instance.view.boot_epoch,
            frame_id=observed.frame.frame_id,
            request_id="guest-text-1",
            # The full-width comma is intentional: this is a Unicode payload.
            text="你好，Palette",  # noqa: RUF001
            method="native_text",
            target="focused",
        )
        assert result.ok is True
        assert result.source == "guest_tool"
        assert result.effect_state == "submitted"
        assert result.method == "native_text"
        assert result.detail == "accepted by the guest service"
        # It advances the action sequence and returns a post-operation frame, exactly like
        # a QMP action, so frames captured before it are fenced.
        assert result.action_seq == 1
        assert result.frame is not None
        assert image is not None and image.startswith(b"\x89PNG")

    async def test_a_guest_operation_fences_earlier_frames(self, hybrid) -> None:
        service, instance = hybrid
        observed = await service.observe("desktop")
        assert observed.frame is not None
        await service.guest_launch_app(
            "desktop",
            instance_id=instance.view.instance_id,
            boot_epoch=instance.view.boot_epoch,
            request_id="guest-launch-1",
            app_id="text-editor",
            options={},
        )
        receipt, _ = await service.act(
            "desktop",
            instance_id=instance.view.instance_id,
            frame_id=observed.frame.frame_id,
            request_id="click-after-launch",
            action={"type": "move", "x": 1, "y": 1},
        )
        assert receipt.ok is False
        assert receipt.error is not None
        assert receipt.error.code == ErrorCode.STALE_FRAME

    async def test_launching_a_registered_app_returns_an_opaque_identifier(self, hybrid) -> None:
        service, instance = hybrid
        result, _ = await service.guest_launch_app(
            "desktop",
            instance_id=instance.view.instance_id,
            boot_epoch=instance.view.boot_epoch,
            request_id="guest-launch-2",
            app_id="text-editor",
            options={},
        )
        assert result.ok is True
        assert result.launch_id == "launch_abc"
        assert result.frame is not None

    async def test_a_stale_boot_epoch_is_refused(self, hybrid) -> None:
        service, instance = hybrid
        with pytest.raises(PaletteError) as info:
            await service.guest_launch_app(
                "desktop",
                instance_id=instance.view.instance_id,
                boot_epoch=instance.view.boot_epoch - 1,
                request_id="guest-launch-3",
                app_id="text-editor",
                options={},
            )
        assert info.value.code is ErrorCode.STALE_FRAME

    async def test_repeating_a_request_id_does_not_type_twice(self, hybrid) -> None:
        service, instance = hybrid
        observed = await service.observe("desktop")
        assert observed.frame is not None
        arguments = {
            "instance_id": instance.view.instance_id,
            "boot_epoch": instance.view.boot_epoch,
            "frame_id": observed.frame.frame_id,
            "request_id": "same-guest-id",
            "text": "hello",
            "method": "native_text",
            "target": "focused",
        }
        first, _ = await service.guest_input_text("desktop", **arguments)
        second, image = await service.guest_input_text("desktop", **arguments)
        assert first.ok is True
        assert second.deduplicated is True
        assert second.action_seq == first.action_seq
        assert image is None
        transport = instance.guest_tools.transport  # type: ignore[attr-defined]
        requests = [message for message in transport.sent if message["kind"] == "request"]
        assert len(requests) == 1

    async def test_a_slow_guest_is_reported_as_an_unknown_outcome(self, hybrid_factory) -> None:
        service, instance = await hybrid_factory(
            responder=guest_service(ignore_requests=True), timeout_seconds=0.15
        )
        result, _ = await service.guest_input_text(
            "desktop",
            instance_id=instance.view.instance_id,
            boot_epoch=instance.view.boot_epoch,
            frame_id=None,
            request_id="slow-guest",
            text="hello",
            method="native_text",
        )
        assert result.ok is False
        assert result.error is not None
        assert result.error.code == ErrorCode.ACTION_OUTCOME_UNKNOWN
        # Not "not_sent": the guest may well have executed it, and the caller must not
        # simply try again.
        assert result.effect_state == "unknown"


class TestConfiguration:
    async def test_the_profile_registry_wins_over_the_guest_list(self, hybrid_factory) -> None:
        """A guest that advertises an application the profile never registered must not
        make it launchable."""
        service, instance = await hybrid_factory(
            responder=guest_service(apps=("text-editor", "file-manager")),
            settings=GuestToolsConfig(
                enabled=True,
                capabilities=["guest.launch_app"],
                input_methods=[],
                allowed_apps={"file-manager": {"executable": "/usr/bin/thunar"}},
            ),
        )
        status = await service.status("desktop")
        assert status.status is not None
        # Only the intersection is reported.
        assert status.status.guest_tools.allowed_apps == ["file-manager"]
        with pytest.raises(PaletteError) as info:
            await service.guest_launch_app(
                "desktop",
                instance_id=instance.view.instance_id,
                boot_epoch=instance.view.boot_epoch,
                request_id="not-registered",
                app_id="text-editor",
                options={},
            )
        assert info.value.code is ErrorCode.APP_NOT_ALLOWED

    async def test_a_capability_the_profile_withheld_is_not_callable(self, hybrid_factory) -> None:
        service, instance = await hybrid_factory(
            settings=GuestToolsConfig(
                enabled=True,
                capabilities=["guest.launch_app"],
                input_methods=["native_text"],
                allowed_apps={"text-editor": {"executable": "/usr/bin/mousepad"}},
            )
        )
        status = await service.status("desktop")
        assert status.status is not None
        assert status.status.guest_tools.capabilities == ["guest.launch_app"]
        with pytest.raises(PaletteError) as info:
            await service.guest_input_text(
                "desktop",
                instance_id=instance.view.instance_id,
                boot_epoch=instance.view.boot_epoch,
                frame_id=None,
                request_id="text-not-granted",
                text="hello",
                method="native_text",
            )
        assert info.value.code is ErrorCode.UNSUPPORTED_CAPABILITY
