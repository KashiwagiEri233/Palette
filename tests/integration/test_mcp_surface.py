"""The MCP surface, driven through the SDK's own tool manager.

These tests call the registered tools the way a client would — through the argument schema
and the result conversion — so a schema mistake, a missing image block or an unvalidated
structured result fails here rather than in a client.
"""

from __future__ import annotations

import base64
import json
from dataclasses import asdict

import pytest

from palette.config import GuestToolsConfig, PaletteConfig
from palette.errors import ErrorCode, PaletteError
from palette.server import build_server
from palette.service import PaletteService
from support.profiles import make_profile


async def call(server, name: str, **arguments):
    return await server._tool_manager.call_tool(name, arguments, None, convert_result=True)


def tool_names(server) -> set[str]:
    return {tool.name for tool in server._tool_manager.list_tools()}


@pytest.fixture
def server(service: PaletteService):
    return build_server(service)


class TestRegistration:
    def test_the_visual_and_lifecycle_tools_are_registered(self, server) -> None:
        assert tool_names(server) == {
            "vm_list",
            "vm_status",
            "vm_observe",
            "vm_act",
            "vm_wait",
            "vm_release_inputs",
            "vm_start",
            "vm_stop",
            "vm_reset",
        }

    def test_guest_tools_are_advertised_when_a_profile_enables_them(
        self, service: PaletteService
    ) -> None:
        profile = service.config.profile("default").model_copy(
            update={
                "guest_tools": GuestToolsConfig(
                    enabled=True,
                    capabilities=["guest.input_text", "guest.launch_app"],
                    input_methods=["native_text"],
                    allowed_apps={"text-editor": {"executable": "/usr/bin/mousepad"}},
                )
            }
        )
        config = PaletteConfig(
            profiles={"default": profile},
            config_root=service.config.config_root,
        ).model_copy(
            update={
                "host": service.config.host.model_copy(
                    update={"runtime_root": str(service.config.runtime_root)}
                )
            }
        )
        hybrid = build_server(PaletteService(config))
        names = tool_names(hybrid)
        assert "vm_guest_input_text" in names
        assert "vm_guest_launch_app" in names

    def test_every_tool_publishes_an_output_schema_covering_success_and_failure(
        self, server
    ) -> None:
        """One schema per tool, with both outcomes expressible, so a client does not have
        to guess at a second undocumented error shape."""
        for tool in server._tool_manager.list_tools():
            schema = tool.output_schema
            assert schema is not None, tool.name
            properties = schema.get("properties", {})
            assert "ok" in properties, tool.name
            assert "error" in properties, tool.name

    def test_read_only_tools_are_annotated_as_such(self, server) -> None:
        tools = {tool.name: tool for tool in server._tool_manager.list_tools()}
        assert tools["vm_observe"].annotations.read_only_hint is True
        assert tools["vm_status"].annotations.read_only_hint is True
        assert tools["vm_list"].annotations.read_only_hint is True
        assert not tools["vm_list"].parameters.get("required")
        assert tools["vm_act"].annotations.read_only_hint is False
        assert tools["vm_reset"].annotations.destructive_hint is True


class TestObservationTools:
    async def test_list_reports_structured_service_errors(
        self, server, service: PaletteService, monkeypatch
    ) -> None:
        async def unavailable():
            raise PaletteError(ErrorCode.STATE_CONFLICT, "runtime is unavailable")

        monkeypatch.setattr(service, "list_vms", unavailable)
        result = await call(server, "vm_list")
        assert result.is_error
        assert result.structured_content["ok"] is False
        assert result.structured_content["error"]["code"] == "STATE_CONFLICT"
        assert result.structured_content["vms"] == []

    async def test_list_observes_control_ownership_without_changing_it(
        self, server, service: PaletteService, running_vm
    ) -> None:
        claim = await service.driver.acquire(
            "desktop", instance_id=running_vm.view.instance_id, run_id="discovery"
        )
        before = asdict(running_vm.view)
        result = await call(server, "vm_list")
        record = result.structured_content["vms"][0]
        assert record["vm_id"] == "desktop"
        assert record["state"] == "RUNNING"
        assert record["persistent"] is False
        assert record["agent_driver_claimed"] is True
        assert record["control_owner"] == "agent"
        assert asdict(running_vm.view) == before
        assert service.driver.claims["desktop"].token == claim["claim_token"]
        lease = await service.control.acquire("desktop", running_vm.view.instance_id)
        result = await call(server, "vm_list")
        record = result.structured_content["vms"][0]
        assert record["control_owner"] == "operator"
        assert record["control_state"] == "held"
        assert record["agent_driver_claimed"] is False
        assert service.control.leases["desktop"].token == lease["lease_id"]
        await service.control.release("desktop", lease["lease_id"])

    async def test_status_for_an_unknown_vm_is_an_error_result(self, server) -> None:
        result = await call(server, "vm_status", vm_id="ghost")
        assert result.is_error is True
        assert result.structured_content["ok"] is False
        assert result.structured_content["error"]["code"] == "VM_NOT_FOUND"

    async def test_observe_returns_an_image_block_and_matching_metadata(
        self, server, running_vm
    ) -> None:
        result = await call(server, "vm_observe", vm_id="desktop")
        assert result.is_error is False
        images = [block for block in result.content if block.type == "image"]
        texts = [block for block in result.content if block.type == "text"]
        assert len(images) == 1
        assert images[0].mime_type == "image/png"
        # The text block mirrors structuredContent, for clients that do not forward images.
        assert len(texts) == 1
        assert json.loads(texts[0].text) == result.structured_content
        # The base64 payload really is a PNG.
        assert base64.b64decode(images[0].data).startswith(b"\x89PNG")

        frame = result.structured_content["frame"]
        assert frame["native_size"]["width"] == 320
        assert frame["image_size"]["width"] == 320
        assert frame["transform_id"] == "full_320x200"
        assert "monotonic_stamp" not in frame

    async def test_observe_on_a_stopped_vm_is_an_error_result(self, server) -> None:
        result = await call(server, "vm_observe", vm_id="desktop")
        assert result.is_error is True
        assert result.structured_content["error"]["code"] == "VM_NOT_FOUND"


class TestActionTool:
    async def test_a_click_returns_a_receipt_and_a_fresh_image(self, server, running_vm) -> None:
        observed = await call(server, "vm_observe", vm_id="desktop")
        frame_id = observed.structured_content["frame"]["frame_id"]
        result = await call(
            server,
            "vm_act",
            vm_id="desktop",
            instance_id=running_vm.view.instance_id,
            frame_id=frame_id,
            request_id="req-1",
            action={"type": "click", "x": 20, "y": 20, "button": "left", "count": 1},
        )
        assert result.is_error is False
        body = result.structured_content
        assert body["ok"] is True
        assert body["effect_state"] == "submitted"
        assert body["input_submitted"] is True
        assert body["observation"] == "captured"
        assert body["action_seq"] == 1
        assert body["source"] == "qmp"
        assert body["backend"] == "qmp-pointer"
        assert body["frame"]["action_seq"] == 1
        assert any(block.type == "image" for block in result.content)

    async def test_a_stale_frame_is_an_error_result_with_the_real_reason(
        self, server, running_vm
    ) -> None:
        observed = await call(server, "vm_observe", vm_id="desktop")
        frame_id = observed.structured_content["frame"]["frame_id"]
        common = {
            "vm_id": "desktop",
            "instance_id": running_vm.view.instance_id,
            "frame_id": frame_id,
            "action": {"type": "move", "x": 4, "y": 4},
        }
        await call(server, "vm_act", request_id="a", **common)
        result = await call(server, "vm_act", request_id="b", **common)
        assert result.is_error is True
        body = result.structured_content
        assert body["ok"] is False
        assert body["error"]["code"] == "STALE_FRAME"
        assert body["effect_state"] == "not_sent"
        # No image is attached to a refusal: there is no frame to show.
        assert not any(block.type == "image" for block in result.content)

    async def test_wait_returns_a_reason_and_a_frame(self, server, running_vm) -> None:
        result = await call(server, "vm_wait", vm_id="desktop", mode="delay", timeout_ms=40)
        assert result.is_error is False
        assert result.structured_content["reason"] == "delay_elapsed"
        assert result.structured_content["frame"] is not None
        assert any(block.type == "image" for block in result.content)


class TestLifecycleTools:
    async def test_an_agent_can_start_a_vm_through_the_tool(self, server) -> None:
        result = await call(server, "vm_start", vm_id="desktop")
        assert result.is_error is False
        assert result.structured_content["ok"] is True
        assert result.structured_content["state"] == "RUNNING"

    async def test_a_caller_supplied_image_backs_the_instance(
        self, server, base_image, tmp_path
    ) -> None:
        result = await call(server, "vm_start", vm_id="desktop", image=str(base_image))
        assert result.is_error is False
        assert result.structured_content["note"] is not None
        assert "image" in result.structured_content["note"]

    async def test_a_missing_caller_image_is_refused_before_anything_starts(
        self, server, tmp_path
    ) -> None:
        result = await call(
            server, "vm_start", vm_id="desktop", image=str(tmp_path / "absent.qcow2")
        )
        assert result.is_error is True
        assert result.structured_content["error"]["code"] == "IMAGE_MISSING"

    async def test_qemu_arguments_reach_the_plan_when_the_profile_allows_them(
        self, server, service
    ) -> None:
        result = await call(server, "vm_start", vm_id="desktop", qemu_args=["-m", "8192"])
        assert result.is_error is False
        instance = service.manager.get("desktop")
        assert "-m" in instance.plan.argv
        assert instance.plan.notes  # the override is recorded on the plan

    async def test_a_closed_switch_refuses_host_material(self, server, service, base_image) -> None:
        from palette.config import LifecyclePolicy
        from support.profiles import make_profile

        service.config.profiles["default"] = make_profile(
            base_image, lifecycle=LifecyclePolicy(allow_agent_images=False)
        )
        result = await call(server, "vm_start", vm_id="desktop", image=str(base_image))
        assert result.is_error is True
        assert result.structured_content["error"]["code"] == "POLICY_DENIED"
        assert result.structured_content["error"]["details"]["kind"] == "image"

    async def test_an_agent_cannot_reset_a_vm_through_the_tool(self, server, running_vm) -> None:
        result = await call(server, "vm_reset", vm_id="desktop")
        assert result.is_error is True
        assert result.structured_content["error"]["code"] == "POLICY_DENIED"

    async def test_an_agent_cannot_force_stop_a_vm(self, server, running_vm) -> None:
        result = await call(server, "vm_stop", vm_id="desktop", force=True)
        assert result.is_error is True
        assert result.structured_content["error"]["code"] == "POLICY_DENIED"

    async def test_a_graceful_stop_is_still_closed_to_agents(self, server, running_vm) -> None:
        """Stopping is not in the permissive set either: the caller may start a VM, not end
        one it did not create."""
        result = await call(server, "vm_stop", vm_id="desktop")
        assert result.is_error is True
        assert result.structured_content["error"]["code"] == "POLICY_DENIED"

    async def test_stopping_an_unknown_vm_is_reported(self, server) -> None:
        result = await call(server, "vm_stop", vm_id="desktop")
        assert result.is_error is True
        assert result.structured_content["error"]["code"] == "VM_NOT_FOUND"


class TestGuestTools:
    """The enhancement tools as a client sees them for a hybrid profile.

    This build ships no guest-side service, so the honest outcome is "unavailable" — and
    crucially not a silent fallback to simulated keystrokes.
    """

    @pytest.fixture
    async def guest_server(self, base_image, runtime_root, tmp_path):
        from palette.audit import NullAuditLog

        profile = make_profile(
            base_image,
            guest_tools=GuestToolsConfig(
                enabled=True,
                capabilities=["guest.input_text", "guest.launch_app"],
                input_methods=["native_text"],
                allowed_apps={"text-editor": {"executable": "/usr/bin/mousepad"}},
            ),
        )
        config = PaletteConfig(profiles={"default": profile}, config_root=tmp_path).model_copy(
            update={
                "host": PaletteConfig().host.model_copy(update={"runtime_root": str(runtime_root)})
            }
        )
        service = PaletteService(config, audit=NullAuditLog())
        await service.startup()
        instance = await service.manager.start("desktop", "default")
        try:
            yield build_server(service), instance
        finally:
            await service.shutdown()

    async def test_status_reports_the_channel_as_unavailable(self, guest_server) -> None:
        server, _instance = guest_server
        result = await call(server, "vm_status", vm_id="desktop")
        guest = result.structured_content["status"]["guest_tools"]
        assert guest["enabled"] is True
        assert guest["service_online"] is False
        assert guest["detail"]
