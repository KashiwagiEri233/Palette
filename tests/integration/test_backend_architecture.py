"""Behavioral acceptance for shared application rules and independent VM operations."""

from __future__ import annotations

import asyncio
import contextlib
import json
import struct
from typing import Any

import pytest

from palette.agent_ipc import AgentCommand, AgentRequest, AgentServer
from palette.desktop import DesktopManager
from palette.errors import ErrorCode, PaletteError
from palette.operator.server import OperatorServer
from palette.runtime import RuntimeOwner
from palette.vm import manager as manager_module
from palette.vm.launcher import LaunchOverrides


def draft(service: Any) -> dict[str, Any]:
    return {
        "name": "Workstation",
        "profile_id": "default",
        "source": "image",
        "path": service.config.profile(None).images.base,
        "vcpus": 1,
        "memory_mib": 512,
        "disk_gib": 24,
        "network": "offline",
    }


async def test_slow_preflight_does_not_block_another_vm_and_prevents_runtime_exit(
    service, monkeypatch
) -> None:
    original = manager_module.probe_binary_async
    entered = asyncio.Event()
    resume = asyncio.Event()
    calls = 0

    async def probe(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await resume.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(manager_module, "probe_binary_async", probe)
    slow = asyncio.create_task(service.start("slow"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert service.manager.find("slow") is None
        with pytest.raises(PaletteError) as busy:
            await service.manager.prepare_runtime_stop()
        assert busy.value.code is ErrorCode.STATE_CONFLICT
        fast = await asyncio.wait_for(service.start("fast"), 10)
        assert fast.ok
        await asyncio.wait_for(service.stop("fast", force=True), 5)
        assert not slow.done()
    finally:
        resume.set()
        await slow


async def test_shutdown_joins_start_cancelled_before_process_allocation(
    service, monkeypatch
) -> None:
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def probe(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(manager_module, "probe_binary_async", probe)
    start = asyncio.create_task(service.start("desktop"))
    await entered.wait()
    await asyncio.wait_for(service.shutdown(), 5)
    assert start.cancelled()
    assert cancelled.is_set()
    assert not service.manager.known()
    with pytest.raises(PaletteError):
        await service.start("another")


@pytest.mark.parametrize("operation", ["restart", "reset"])
async def test_persistent_restart_and_reset_exclude_competing_start(
    service, monkeypatch, operation
) -> None:
    desktop = DesktopManager(service)
    vm_id = (await desktop.create(draft(service)))["vm_id"]
    await desktop.start(vm_id)
    disk = service.manager.get(vm_id).paths.overlay
    original = desktop.stop
    stopped = asyncio.Event()
    resume = asyncio.Event()

    async def stop(*args, **kwargs):
        result = await original(*args, **kwargs)
        stopped.set()
        await resume.wait()
        return result

    monkeypatch.setattr(desktop, "stop", stop)
    restarting = asyncio.create_task(getattr(desktop, operation)(vm_id))
    competitor = None
    try:
        await asyncio.wait_for(stopped.wait(), 10)
        competitor = asyncio.create_task(service.manager.start(vm_id))
        await asyncio.sleep(0)
        assert not competitor.done()
    finally:
        resume.set()
        await restarting
        if competitor is not None:
            with pytest.raises(PaletteError) as duplicate:
                await competitor
            assert duplicate.value.code is ErrorCode.VM_ALREADY_RUNNING
    current = service.manager.get(vm_id)
    assert (current.paths.overlay == disk) is (operation == "restart")
    assert disk.exists()


async def test_inflight_start_protects_another_vms_old_backing_layer(service, monkeypatch) -> None:
    source = await service.manager.start("source")
    await service.manager.stop("source")
    profile = service.config.profile(None)
    prune_profile = profile.model_copy(
        update={"retention": profile.retention.model_copy(update={"keep_overlays": False})}
    )
    entered = asyncio.Event()

    async def probe(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(manager_module, "probe_binary_async", probe)
    start = asyncio.create_task(
        service.manager.start(
            "dependent", overrides=LaunchOverrides(image=source.paths.overlay, image_format="qcow2")
        )
    )
    try:
        await entered.wait()
        assert service.manager.find("dependent") is None
        service.manager._apply_retention("source", prune_profile)
        assert source.paths.overlay.exists()
    finally:
        start.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await start
    service.manager._apply_retention("source", prune_profile)
    assert not source.paths.root.exists()


async def test_stale_stop_does_not_revoke_current_operator_control(service) -> None:
    instance = await service.manager.start("desktop")
    lease = await service.control.acquire("desktop", instance.view.instance_id)
    epoch = instance.view.controller_epoch
    with pytest.raises(PaletteError) as stale:
        await service.stop("desktop", force=True, expected_instance_id="inst_previous")
    assert stale.value.code is ErrorCode.STATE_CONFLICT
    assert service.control.validate("desktop", lease["lease_id"])
    assert instance.view.controller_epoch == epoch
    assert instance.process.returncode is None


async def test_queued_release_cannot_revoke_a_replacement_lease(service) -> None:
    instance = await service.manager.start("desktop")
    old = await service.control.acquire("desktop", instance.view.instance_id)

    async def replace():
        async with service.manager.operations.hold("desktop"):
            await service.control.release("desktop", old["lease_id"])
            return await service.control.acquire("desktop", instance.view.instance_id)

    async with service.manager.operations.hold("desktop"):
        replacement = asyncio.create_task(replace())
        await asyncio.sleep(0)
        stale_release = asyncio.create_task(service.control.release("desktop", old["lease_id"]))
        await asyncio.sleep(0)
    current = await asyncio.wait_for(replacement, 5)
    with pytest.raises(PaletteError) as stale:
        await stale_release
    assert stale.value.code is ErrorCode.POLICY_DENIED
    assert service.control.validate("desktop", current["lease_id"])


async def test_control_takeover_for_one_vm_does_not_block_another(service, monkeypatch) -> None:
    first, second = await asyncio.gather(
        service.manager.start("first"), service.manager.start("second")
    )
    entered = asyncio.Event()
    resume = asyncio.Event()
    original = service.take_control

    async def takeover(vm_id, **kwargs):
        if vm_id == "first":
            entered.set()
            await resume.wait()
        return await original(vm_id, **kwargs)

    monkeypatch.setattr(service, "take_control", takeover)
    slow = asyncio.create_task(service.control.acquire("first", first.view.instance_id))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        lease = await asyncio.wait_for(
            service.control.acquire("second", second.view.instance_id), 5
        )
        assert service.control.validate("second", lease["lease_id"])
        assert not slow.done()
    finally:
        resume.set()
        await slow


async def test_runtime_attempts_every_cleanup_step_after_failure(service, monkeypatch) -> None:
    owner = RuntimeOwner(service)
    calls = []
    shutdown = service.shutdown

    async def display_close():
        calls.append("display")
        raise OSError("injected display failure")

    async def service_close():
        calls.append("service")
        await shutdown()

    monkeypatch.setattr(owner.display, "close", display_close)
    monkeypatch.setattr(service, "shutdown", service_close)
    monkeypatch.setattr(owner.operator, "release", lambda: calls.append("lease"))
    for _ in range(2):
        with pytest.raises(OSError, match="injected"):
            await owner.close()
    assert calls == ["display", "service", "lease"]


class MemoryChannel:
    def __init__(self, request: AgentRequest) -> None:
        payload = request.model_dump_json().encode()
        self.pending = bytearray(struct.pack(">I", len(payload)) + payload)
        self.sent = bytearray()

    async def readexactly(self, size: int) -> bytes:
        data = bytes(self.pending[:size])
        del self.pending[:size]
        return data

    async def write(self, data: bytes) -> None:
        self.sent.extend(data)

    async def close(self) -> None:
        pass

    def response(self) -> dict[str, Any]:
        size = struct.unpack(">I", self.sent[:4])[0]
        assert self.sent[4 + size :] == b"\0\0\0\0"
        return json.loads(self.sent[4 : 4 + size])


async def test_authenticated_agent_conflict_returns_structured_error(service) -> None:
    server = AgentServer(service, OperatorServer(service))
    request = AgentRequest(
        generation=server.endpoint.generation,
        token=server.endpoint.token,
        request_id="a" * 32,
        command=AgentCommand(method="status", parameters={"vm_id": "first"}),
    )
    first = MemoryChannel(request)
    await server._handle(first)
    assert first.response()["ok"]
    changed = request.model_copy(
        update={"command": AgentCommand(method="status", parameters={"vm_id": "second"})}
    )
    second = MemoryChannel(changed)
    await server._handle(second)
    assert second.response()["error"]["code"] == ErrorCode.REQUEST_ID_CONFLICT
    await server.stop()
