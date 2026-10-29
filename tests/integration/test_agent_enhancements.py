"""Agent tools use standard guest channels without dialogs or operator leases."""

from __future__ import annotations

import base64
import io
from unittest.mock import AsyncMock, Mock

import pytest

from conftest import read_command_log
from palette.agent_ipc import AgentServer, RemoteService
from palette.config import GuestDesktopConfig, QemuGuestAgentConfig
from palette.errors import ErrorCode, PaletteError
from palette.guest_agent import QemuGuestAgent
from palette.guest_desktop import RfbGuestDesktop
from palette.operator.server import OperatorServer
from palette.server import build_server


@pytest.fixture
def enhanced(service, running_vm):
    profile = running_vm.profile.model_copy(
        update={
            "guest_desktop": GuestDesktopConfig(clipboard=True),
            "guest_agent": QemuGuestAgentConfig(enabled=True),
        }
    )
    running_vm.profile = profile
    running_vm.policy.profile = profile
    service.config.profiles[profile.profile_id] = profile
    running_vm.plan.clipboard_enabled = True
    running_vm.enhancement_ports["palette-vdagent"] = True
    desktop = RfbGuestDesktop(("127.0.0.1", 1), "test-password")
    desktop.writer = Mock()
    desktop.writer.is_closing.return_value = False
    desktop.task = Mock()
    desktop.task.done.return_value = False
    desktop.clipboard_set = AsyncMock(return_value={"submitted": True})
    desktop.clipboard_get = AsyncMock(return_value={"text": "客户机内容 😀"})
    desktop.close = AsyncMock()
    running_vm.guest_desktop = desktop
    agent = QemuGuestAgent("test-only")
    agent.online = True
    agent.commands = {"guest-file-open", "guest-file-read", "guest-file-write", "guest-file-close"}
    files: dict[str, bytes] = {}
    handles: dict[int, tuple[str, io.BytesIO]] = {}
    counter = 0

    async def command(name, arguments):
        nonlocal counter
        if name == "guest-file-open":
            counter += 1
            path = arguments["path"]
            if arguments["mode"] == "wb":
                files[path] = b""
            handles[counter] = (path, io.BytesIO(files[path]))
            return counter
        path, stream = handles[arguments["handle"]]
        if name == "guest-file-close":
            del handles[arguments["handle"]]
            return {}
        if name == "guest-file-write":
            data = base64.b64decode(arguments["buf-b64"])
            stream.write(data)
            files[path] = stream.getvalue()
            return {"count": len(data)}
        data = stream.read(arguments["count"])
        return {
            "count": len(data),
            "buf-b64": base64.b64encode(data).decode(),
            "eof": stream.tell() == len(files[path]),
        }

    replies = []

    async def send(name, arguments):
        replies.append(await command(name, arguments))

    agent._synchronize = AsyncMock()
    agent._send = AsyncMock(side_effect=send)
    agent._reply = AsyncMock(side_effect=lambda: replies.pop(0))
    running_vm.qemu_guest_agent = agent
    return service, running_vm, desktop, agent, files


async def invoke(server, name, instance, request_id, **parameters):
    return await server._tool_manager.call_tool(
        name,
        {
            "vm_id": "desktop",
            "instance_id": instance.view.instance_id,
            "boot_epoch": instance.view.boot_epoch,
            "request_id": request_id,
            **parameters,
        },
        None,
        convert_result=True,
    )


async def test_unicode_input_uses_standard_clipboard_then_paste_and_deduplicates(enhanced):
    service, instance, desktop, _, _ = enhanced
    server = build_server(service)
    tools = {tool.name for tool in server._tool_manager.list_tools()}
    assert {"vm_guest_input_text", "vm_guest_clipboard_get", "vm_guest_file_write"} <= tools
    assert "vm_guest_launch_app" not in tools
    observed = await service.observe("desktop")
    text = "中文输入\n第二行 😀"
    result = await invoke(
        server,
        "vm_guest_input_text",
        instance,
        "paste",
        text=text,
        frame_id=observed.frame.frame_id,
    )
    assert not result.is_error
    assert result.structured_content["effect_state"] == "submitted"
    assert any(item.type == "image" for item in result.content)
    desktop.clipboard_set.assert_awaited_once_with(text)
    events = [
        event
        for command in read_command_log(instance)
        if command["command"] == "input-send-event"
        for event in command["arguments"]["events"]
    ]
    assert any(event.get("data", {}).get("key", {}).get("data") == "v" for event in events)
    again = await invoke(
        server,
        "vm_guest_input_text",
        instance,
        "paste",
        text=text,
        frame_id=observed.frame.frame_id,
    )
    assert again.structured_content["deduplicated"]
    assert desktop.clipboard_set.await_count == 1
    assert not service.control.leases
    status = await service.status("desktop")
    assert "clipboard_paste" in status.status.guest_tools.input_methods


async def test_binary_transfer_round_trip_and_chunk_deduplication(enhanced):
    service, instance, _, agent, files = enhanced
    server = build_server(service)
    for tool in server._tool_manager.list_tools():
        assert tool.output_schema and "ok" in tool.output_schema["properties"]
    opened = await invoke(
        server, "vm_guest_file_open", instance, "open-write", path="/home/guest/中文.bin", mode="w"
    )
    token = opened.structured_content["data"]["transfer_id"]
    encoded = base64.b64encode(b"\0\xff\x01" + "文件 😀".encode()).decode()
    for _ in range(2):
        written = await invoke(
            server, "vm_guest_file_write", instance, "write", transfer_id=token, data_base64=encoded
        )
        assert not written.is_error
    assert written.structured_content["deduplicated"]
    assert files["/home/guest/中文.bin"] == base64.b64decode(encoded)
    await invoke(server, "vm_guest_file_close", instance, "close-write", transfer_id=token)
    opened = await invoke(
        server, "vm_guest_file_open", instance, "open-read", path="/home/guest/中文.bin"
    )
    token = opened.structured_content["data"]["transfer_id"]
    first = await invoke(
        server, "vm_guest_file_read", instance, "read-1", transfer_id=token, count=2
    )
    repeated = await invoke(
        server, "vm_guest_file_read", instance, "read-1", transfer_id=token, count=2
    )
    assert first.structured_content["data"] == repeated.structured_content["data"]
    rest = await invoke(server, "vm_guest_file_read", instance, "read-2", transfer_id=token)
    assert rest.structured_content["data"]["eof"]
    assert (
        base64.b64decode(first.structured_content["data"]["data_base64"])
        + base64.b64decode(rest.structured_content["data"]["data_base64"])
        == files["/home/guest/中文.bin"]
    )
    await invoke(server, "vm_guest_file_close", instance, "close-read", transfer_id=token)
    assert not agent.transfers


async def test_unavailable_and_stale_requests_preserve_baseline(enhanced):
    service, instance, desktop, _, _ = enhanced
    server = build_server(service)
    desktop.writer = None
    result = await invoke(server, "vm_guest_clipboard_get", instance, "offline")
    assert result.is_error and result.structured_content["effect_state"] == "not_sent"
    assert result.structured_content["error"]["code"] == "GUEST_TOOL_UNAVAILABLE"
    desktop.clipboard_get.assert_not_awaited()
    desktop.writer = Mock()
    desktop.writer.is_closing.return_value = False
    result = await invoke(server, "vm_guest_clipboard_get", instance, "recovered")
    assert result.structured_content["data"]["text"] == "客户机内容 😀"
    result = await invoke(
        server, "vm_guest_clipboard_set", instance, "old-boot", boot_epoch=-1, text="old"
    )
    assert result.is_error and result.structured_content["error"]["code"] == "STALE_FRAME"
    observed = await service.observe("desktop")
    receipt, _ = await service.act(
        "desktop",
        instance_id=instance.view.instance_id,
        frame_id=observed.frame.frame_id,
        request_id="baseline",
        action={"type": "key", "keys": ["A"]},
    )
    assert receipt.ok


async def test_invalid_and_unsupported_data_are_rejected_before_sending(enhanced):
    service, instance, desktop, agent, _ = enhanced
    server = build_server(service)
    result = await invoke(server, "vm_guest_clipboard_set", instance, "oversize", text="中" * 20000)
    assert result.is_error and result.structured_content["effect_state"] == "not_sent"
    desktop.clipboard_set.assert_not_awaited()
    result = await invoke(
        server, "vm_guest_file_open", instance, "relative", path="relative/file", mode="w"
    )
    assert result.is_error
    agent.commands.remove("guest-file-close")
    result = await invoke(
        server, "vm_guest_file_open", instance, "no-close", path="/home/guest/file", mode="w"
    )
    assert result.is_error and result.structured_content["effect_state"] == "not_sent"
    agent._send.assert_not_awaited()
    with pytest.raises(PaletteError):
        await service.guest_enhancement(
            "desktop",
            instance_id=instance.view.instance_id,
            boot_epoch=instance.view.boot_epoch,
            request_id="shell",
            operation="guest-exec",
            parameters={},
        )


@pytest.mark.parametrize(
    ("operation", "parameters", "transfers", "error_code"),
    [
        ("read", {"transfer_id": "missing"}, {}, ErrorCode.INVALID_ARGUMENT),
        (
            "read",
            {"transfer_id": "writer"},
            {"writer": (1, "w", 0)},
            ErrorCode.INVALID_ARGUMENT,
        ),
        (
            "write",
            {"transfer_id": "missing", "data_base64": "eA=="},
            {},
            ErrorCode.INVALID_ARGUMENT,
        ),
        (
            "write",
            {"transfer_id": "full", "data_base64": "eA=="},
            {"full": (1, "w", 5)},
            ErrorCode.INVALID_ARGUMENT,
        ),
        (
            "open",
            {"path": "/home/guest/new", "mode": "w"},
            {str(index): (index, "w", 0) for index in range(4)},
            ErrorCode.BUSY,
        ),
    ],
)
async def test_local_file_errors_are_not_sent_and_allow_corrected_requests(
    enhanced, operation, parameters, transfers, error_code
):
    service, instance, _, agent, _ = enhanced
    server = build_server(service)
    agent.max_file_bytes = 5
    agent.transfers.update(transfers)
    sequence = instance.view.action_seq
    result = await invoke(server, "vm_guest_file_" + operation, instance, "invalid", **parameters)
    assert result.is_error
    assert result.structured_content["effect_state"] == "not_sent"
    assert result.structured_content["error"]["code"] == error_code
    assert result.structured_content["action_seq"] == sequence
    assert instance.view.action_seq == sequence
    agent._send.assert_not_awaited()
    agent.transfers.clear()
    corrected = await invoke(
        server, "vm_guest_file_open", instance, "corrected", path="/home/guest/new", mode="w"
    )
    assert not corrected.is_error
    assert corrected.structured_content["effect_state"] == "submitted"
    assert corrected.structured_content["action_seq"] == sequence + 1
    agent._send.assert_awaited_once()


async def test_qga_reply_loss_is_unknown_and_not_replayed(enhanced):
    service, instance, _, agent, _ = enhanced
    server = build_server(service)
    agent._reply.side_effect = TimeoutError
    sequence = instance.view.action_seq
    for attempt in range(2):
        result = await invoke(
            server, "vm_guest_file_open", instance, "lost-reply", path="/home/guest/new", mode="w"
        )
        assert result.is_error
        assert result.structured_content["effect_state"] == "unknown"
        assert result.structured_content["action_seq"] == sequence + 1
        assert result.structured_content["deduplicated"] == bool(attempt)
    agent._send.assert_awaited_once()


async def test_qga_sync_failure_does_not_mark_file_request_sent(enhanced):
    service, instance, _, agent, _ = enhanced
    agent._synchronize.side_effect = ConnectionError
    sequence = instance.view.action_seq
    result = await invoke(
        build_server(service),
        "vm_guest_file_open",
        instance,
        "sync-failed",
        path="/home/guest/new",
        mode="w",
    )
    assert result.is_error
    assert result.structured_content["effect_state"] == "not_sent"
    assert result.structured_content["action_seq"] == sequence
    agent._send.assert_not_awaited()


async def test_qga_close_can_retry_when_sync_fails_before_sending(enhanced):
    service, instance, _, agent, _ = enhanced
    server = build_server(service)
    opened = await invoke(
        server, "vm_guest_file_open", instance, "open-to-close", path="/home/guest/file", mode="w"
    )
    token = opened.structured_content["data"]["transfer_id"]
    sequence = instance.view.action_seq
    agent._send.reset_mock()
    agent._synchronize.side_effect = ConnectionError
    failed = await invoke(
        server, "vm_guest_file_close", instance, "unsent-close", transfer_id=token
    )
    assert failed.is_error and failed.structured_content["effect_state"] == "not_sent"
    assert failed.structured_content["action_seq"] == sequence
    assert token in agent.transfers
    agent._send.assert_not_awaited()

    agent.online = True
    agent._synchronize.side_effect = None
    closed = await invoke(server, "vm_guest_file_close", instance, "retry-close", transfer_id=token)
    assert not closed.is_error and closed.structured_content["effect_state"] == "submitted"
    assert closed.structured_content["action_seq"] == sequence + 1
    assert token not in agent.transfers
    agent._send.assert_awaited_once_with("guest-file-close", {"handle": 1})


async def test_qga_close_lost_reply_is_unknown_and_not_replayed(enhanced):
    service, instance, _, agent, _ = enhanced
    server = build_server(service)
    opened = await invoke(
        server, "vm_guest_file_open", instance, "open-to-close", path="/home/guest/file", mode="w"
    )
    token = opened.structured_content["data"]["transfer_id"]
    agent._send.reset_mock()
    agent._reply.side_effect = TimeoutError
    sequence = instance.view.action_seq
    for attempt in range(2):
        result = await invoke(
            server, "vm_guest_file_close", instance, "lost-close", transfer_id=token
        )
        assert result.is_error and result.structured_content["effect_state"] == "unknown"
        assert result.structured_content["action_seq"] == sequence + 1
        assert result.structured_content["deduplicated"] == bool(attempt)
    assert token not in agent.transfers
    agent._send.assert_awaited_once_with("guest-file-close", {"handle": 1})


async def test_unknown_write_is_retained_and_never_replayed(enhanced):
    service, instance, desktop, _, _ = enhanced
    desktop.clipboard_set.side_effect = PaletteError(ErrorCode.ACTION_OUTCOME_UNKNOWN, "unknown")
    server = build_server(service)
    for _ in range(2):
        result = await invoke(server, "vm_guest_clipboard_set", instance, "unknown", text="中文")
        assert result.is_error and result.structured_content["effect_state"] == "unknown"
    desktop.clipboard_set.assert_awaited_once()


async def test_operator_takeover_closes_agent_files_and_refuses_new_agent_calls(enhanced):
    service, instance, desktop, agent, _ = enhanced
    server = build_server(service)
    opened = await invoke(
        server, "vm_guest_file_open", instance, "open", path="/home/guest/file", mode="w"
    )
    assert opened.structured_content["data"]["transfer_id"] in agent.transfers
    lease = await service.control.acquire("desktop", instance.view.instance_id)
    assert not agent.transfers
    result = await invoke(server, "vm_guest_clipboard_set", instance, "busy", text="no")
    assert result.is_error and result.structured_content["error"]["code"] == "BUSY"
    desktop.clipboard_set.assert_not_awaited()
    await service.control.release("desktop", lease["lease_id"])


async def test_remote_mcp_uses_authenticated_agent_ipc(enhanced):
    service, instance, _, _, _ = enhanced
    async with OperatorServer(service) as operator:
        agent_server = AgentServer(service, operator)
        await agent_server.start()
        try:
            server = build_server(RemoteService(service.config, agent_server.endpoint))
            result = await invoke(server, "vm_guest_clipboard_get", instance, "remote")
            assert result.structured_content["data"]["text"] == "客户机内容 😀"
        finally:
            await agent_server.stop()


async def test_run_claim_fences_enhancements_and_releasing_it_closes_files(enhanced):
    service, instance, _, agent, _ = enhanced
    claim = await service.driver.acquire(
        "desktop", instance_id=instance.view.instance_id, run_id="enhanced-run"
    )
    parameters = {
        "instance_id": instance.view.instance_id,
        "boot_epoch": instance.view.boot_epoch,
        "request_id": "claim-open",
        "operation": "file.open",
        "parameters": {"path": "/home/guest/file", "mode": "w"},
    }
    with pytest.raises(PaletteError) as failure:
        await service.guest_enhancement("desktop", **parameters)
    assert failure.value.code == ErrorCode.BUSY
    with service.driver.bind(claim["claim_token"]):
        result, _ = await service.guest_enhancement("desktop", **parameters)
        assert result.ok and result.data["transfer_id"] in agent.transfers
        assert (await service.driver.release("desktop"))["release_confirmed"]
    assert not agent.transfers
