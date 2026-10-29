"""Windows enhancements retain shared scheduling, identity and input cleanup."""

from unittest.mock import AsyncMock

import pytest

from conftest import read_command_log
from palette.agent_ipc import AgentServer, RemoteService
from palette.capture.qmp import QmpCaptureBackend
from palette.config import GuestDesktopConfig, GuestToolsConfig
from palette.errors import ErrorCode, PaletteError
from palette.guest_tools.base import GuestToolOutcome, MockGuestToolBackend
from palette.models import EffectState
from palette.operator.server import OperatorServer
from palette.server import build_server


@pytest.fixture
def windows_vm(service, running_vm):
    profile = running_vm.profile.model_copy(
        update={
            "guest_tools": GuestToolsConfig(),
            "guest_desktop": GuestDesktopConfig(clipboard=True),
        }
    )
    running_vm.profile = profile
    running_vm.policy.profile = profile
    service.config.profiles[profile.profile_id] = profile
    running_vm.guest_tools = MockGuestToolBackend(
        operation_data={
            "filesystem": {"content": "客户机内容 😀"},
            "snapshot": {
                "snapshot_id": "snap1",
                "virtual_screen": {
                    "left": 0,
                    "top": 0,
                    "width": 320,
                    "height": 200,
                },
                "elements": [{"label": 0, "name": "input"}],
            },
            "resolve_elements": {"points": [[80, 60]]},
            "clipboard.get": {"text": "guest text"},
        }
    )
    return service, running_vm


async def invoke(service, instance, operation, request_id, parameters, **kwargs):
    return await service.guest_enhancement(
        "desktop",
        instance_id=instance.view.instance_id,
        boot_epoch=instance.view.boot_epoch,
        request_id=request_id,
        operation=operation,
        parameters=parameters,
        **kwargs,
    )


async def test_new_mcp_tools_and_data_receipts_share_existing_channel(windows_vm):
    service, instance = windows_vm
    server = build_server(service)
    names = {tool.name for tool in server._tool_manager.list_tools()}
    assert {
        "vm_guest_" + name
        for name in (
            "filesystem",
            "powershell",
            "process",
            "registry",
            "app",
            "snapshot",
            "multi_select",
            "multi_edit",
        )
    } <= names
    arguments = {
        "vm_id": "desktop",
        "instance_id": instance.view.instance_id,
        "boot_epoch": instance.view.boot_epoch,
        "request_id": "read",
        "mode": "read",
        "path": "file.txt",
    }
    result = await server._tool_manager.call_tool(
        "vm_guest_filesystem", arguments, None, convert_result=True
    )
    assert not result.is_error
    assert result.structured_content["data"]["content"] == "客户机内容 😀"
    repeated = await server._tool_manager.call_tool(
        "vm_guest_filesystem", arguments, None, convert_result=True
    )
    assert repeated.structured_content["deduplicated"]
    assert len(instance.guest_tools.calls) == 1


async def test_helper_clipboard_works_without_spice_and_stays_guest_only(windows_vm):
    service, instance = windows_vm
    instance.guest_desktop = None
    result, _ = await invoke(service, instance, "clipboard.get", "clipboard", {})
    assert result.ok and result.data["text"] == "guest text"
    status = await service.status("desktop")
    assert status.status.enhancements["desktop"]["clipboard"]
    assert "guest.app" in status.status.enhancements["windows"]["capabilities"]


@pytest.mark.parametrize("effect_state", [EffectState.NOT_SENT, EffectState.UNKNOWN])
async def test_clipboard_paste_preserves_helper_failure_without_pasting(windows_vm, effect_state):
    service, instance = windows_vm
    failure = GuestToolOutcome(
        ok=False,
        effect_state=effect_state,
        detail="clipboard is unavailable",
        error=PaletteError(ErrorCode.GUEST_TOOL_UNAVAILABLE, "clipboard write failed"),
    )

    async def execute_operation(*, operation, parameters, on_sending):
        assert operation == "clipboard.set"
        assert parameters == {"text": "new clipboard text"}
        if effect_state is not EffectState.NOT_SENT:
            on_sending()
        return failure

    instance.guest_tools.execute_operation = AsyncMock(side_effect=execute_operation)
    frame = (await service.observe("desktop")).frame.frame_id
    sequence = instance.view.action_seq
    for attempt in range(2):
        result, _ = await service.guest_input_text(
            "desktop",
            instance_id=instance.view.instance_id,
            boot_epoch=instance.view.boot_epoch,
            frame_id=frame,
            request_id="failed-paste",
            text="new clipboard text",
            method="clipboard_paste",
        )
        assert not result.ok
        assert result.effect_state is effect_state
        assert result.error.code == ErrorCode.GUEST_TOOL_UNAVAILABLE
        assert result.detail == failure.detail
        assert result.deduplicated == bool(attempt)
        assert result.action_seq == sequence + (effect_state is not EffectState.NOT_SENT)
    instance.guest_tools.execute_operation.assert_awaited_once()
    assert not any(item["command"] == "input-send-event" for item in read_command_log(instance))


async def test_bounded_large_uia_data_crosses_authenticated_agent_ipc(windows_vm):
    service, instance = windows_vm
    data = instance.guest_tools.operation_data["snapshot"]
    data["elements"] = [{"label": index, "name": "中" * 250} for index in range(500)]
    async with OperatorServer(service) as operator:
        agent = AgentServer(service, operator)
        await agent.start()
        try:
            remote = RemoteService(service.config, agent.endpoint)
            result, image = await remote.guest_enhancement(
                "desktop",
                instance_id=instance.view.instance_id,
                boot_epoch=instance.view.boot_epoch,
                request_id="large-snapshot",
                operation="snapshot",
                parameters={},
            )
            assert result.ok and image
            assert len(result.data["elements"]) == 500
            assert result.data["elements"][-1]["name"] == "中" * 250
        finally:
            await agent.stop()


async def test_batch_validates_all_targets_before_sending_and_releases_ctrl(windows_vm):
    service, instance = windows_vm
    frame = (await service.observe("desktop")).frame.frame_id
    invalid, _ = await invoke(
        service, instance, "multi_select", "invalid", {"locs": [[5, 5], [999, 5]]}, frame_id=frame
    )
    assert not invalid.ok and invalid.effect_state == "not_sent"
    assert not any(item["command"] == "input-send-event" for item in read_command_log(instance))
    result, image = await invoke(
        service, instance, "multi_select", "select", {"locs": [[5, 5], [30, 20]]}, frame_id=frame
    )
    assert result.ok and result.data == {"completed": 2, "total": 2}
    assert image
    assert service.controller_for(instance).ledger_snapshot() == {"keys": [], "buttons": []}
    before = len(read_command_log(instance))
    repeat, _ = await invoke(
        service, instance, "multi_select", "select", {"locs": [[5, 5], [30, 20]]}, frame_id=frame
    )
    assert repeat.deduplicated
    assert len(read_command_log(instance)) == before


async def test_labels_are_bound_to_snapshot_frame_and_resolved_again(windows_vm):
    service, instance = windows_vm
    snap, _ = await invoke(service, instance, "snapshot", "snapshot", {})
    assert snap.data["labels_usable"]
    backend = instance.guest_tools
    backend.input_text = AsyncMock(return_value=GuestToolOutcome(ok=True))
    result, _ = await invoke(
        service,
        instance,
        "multi_edit",
        "edit",
        {
            "labels": [[0, "中文 😀"]],
            "snapshot_id": "snap1",
        },
        frame_id=snap.frame.frame_id,
    )
    assert result.ok and result.data["completed"] == 1
    backend.input_text.assert_awaited_once()
    assert backend.input_text.call_args.kwargs["text"] == "中文 😀"
    assert len([entry for entry in backend.calls if entry[0] == "resolve_elements"]) == 2
    new_frame = (await service.observe("desktop")).frame.frame_id
    rejected, _ = await invoke(
        service,
        instance,
        "multi_select",
        "stale",
        {
            "labels": [0],
            "snapshot_id": "snap1",
        },
        frame_id=new_frame,
    )
    assert rejected.error.code == "STALE_FRAME"


async def test_batch_reports_partial_progress_without_replaying_text(windows_vm):
    service, instance = windows_vm
    backend = instance.guest_tools

    async def text_input(*, text, method, on_sending):
        on_sending()
        if text == "two":
            raise PaletteError(ErrorCode.ACTION_OUTCOME_UNKNOWN, "lost acknowledgement")
        return GuestToolOutcome(ok=True)

    backend.input_text = AsyncMock(side_effect=text_input)
    frame = (await service.observe("desktop")).frame.frame_id
    result, _ = await invoke(
        service,
        instance,
        "multi_edit",
        "partial",
        {
            "locs": [[10, 10, "one"], [20, 20, "two"], [30, 30, "three"]],
        },
        frame_id=frame,
    )
    assert not result.ok and result.data == {"completed": 1, "total": 3}
    assert result.effect_state == "unknown"
    assert backend.input_text.await_count == 2
    assert service.controller_for(instance).ledger_snapshot() == {"keys": [], "buttons": []}
    backend.service_online = False
    repeated, _ = await invoke(
        service,
        instance,
        "multi_edit",
        "partial",
        {"locs": [[10, 10, "one"], [20, 20, "two"], [30, 30, "three"]]},
        frame_id=frame,
    )
    assert repeated.deduplicated and repeated.effect_state == "unknown"
    assert backend.input_text.await_count == 2


async def test_labels_keep_native_precision_with_a_downscaled_image(windows_vm):
    service, instance = windows_vm
    controller = service.controller_for(instance)
    controller._capture = QmpCaptureBackend(
        controller._qmp,
        controller.profile.capture.model_copy(update={"max_long_edge": 256}),
    )
    instance.guest_tools.operation_data["resolve_elements"] = {"points": [[1, 1]]}
    snapshot, _ = await invoke(service, instance, "snapshot", "scaled-snapshot", {})
    assert snapshot.frame.image_size.width == 256
    result, _ = await invoke(
        service,
        instance,
        "multi_select",
        "scaled-select",
        {
            "labels": [0],
            "snapshot_id": "snap1",
            "press_ctrl": False,
        },
        frame_id=snapshot.frame.frame_id,
    )
    assert result.ok
    points = [
        event["data"]["value"]
        for item in read_command_log(instance)
        if item["command"] == "input-send-event"
        for event in item["arguments"]["events"]
        if event["type"] == "abs" and event["data"]["axis"] == "x"
    ]
    assert points == [round(32767 / 319)]
    assert service.controller_for(instance).ledger_snapshot() == {"keys": [], "buttons": []}


async def test_capability_and_human_ownership_are_checked_before_dispatch(windows_vm):
    service, instance = windows_vm
    instance.policy.profile = instance.profile.model_copy(
        update={
            "guest_tools": GuestToolsConfig(capabilities=["guest.input_text"]),
        }
    )
    rejected, _ = await invoke(service, instance, "app", "denied", {"name": "Notepad"})
    assert rejected.error.code == "UNSUPPORTED_CAPABILITY"
    assert not instance.guest_tools.calls
    await service.control.acquire("desktop", instance.view.instance_id)
    with pytest.raises(PaletteError) as caught:
        await invoke(service, instance, "filesystem", "operator", {"mode": "info", "path": "file"})
    assert caught.value.code == ErrorCode.BUSY
