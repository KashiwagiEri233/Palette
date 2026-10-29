"""Unresponsive guests never trap the explicitly authorized force-stop path."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from conftest import read_command_log, write_behavior
from palette.desktop import DesktopManager
from palette.errors import ErrorCode, PaletteError
from palette.guest_agent import QemuGuestAgent
from palette.vm.manager import VmState


def budget(instance, seconds):
    instance.profile = instance.profile.model_copy(
        update={
            "timeouts": instance.profile.timeouts.model_copy(update={"shutdown_seconds": seconds})
        }
    )


async def stopping(instance):
    async with asyncio.timeout(3):
        while instance.state is not VmState.STOPPING:  # noqa: ASYNC110 - observe the lifecycle state
            await asyncio.sleep(0.01)


async def test_force_interrupts_an_inflight_graceful_shutdown(service, running_vm):
    budget(running_vm, 60)
    write_behavior(running_vm, {"ignore_powerdown": True})
    initial = running_vm.paths.overlay.read_bytes()
    graceful = asyncio.create_task(service.manager.graceful_stop("desktop"))
    try:
        await stopping(running_vm)
        await asyncio.wait_for(
            service.manager.stop(
                "desktop",
                force=True,
                expected_instance_id=running_vm.view.instance_id,
            ),
            4,
        )
        await asyncio.wait_for(graceful, 1)
        assert running_vm.state is VmState.STOPPED
        assert running_vm.process.returncode is not None
        assert running_vm.paths.overlay.read_bytes() == initial
    finally:
        if not graceful.done():
            graceful.cancel()
            await asyncio.gather(graceful, return_exceptions=True)


async def test_force_can_interrupt_control_cleanup_before_the_power_request(
    service, running_vm, monkeypatch
):
    entered = asyncio.Event()

    async def revoke(vm_id):
        entered.set()
        await running_vm.process.wait()
        return True

    monkeypatch.setattr(service.control, "revoke", revoke)
    normal = asyncio.create_task(service.stop("desktop", actor="operator"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert running_vm.state is VmState.RUNNING
        await asyncio.wait_for(
            service.stop(
                "desktop",
                force=True,
                actor="operator",
                expected_instance_id=running_vm.view.instance_id,
            ),
            4,
        )
        await asyncio.wait_for(normal, 1)
        assert running_vm.process.returncode is not None
        assert running_vm.state is VmState.STOPPED
    finally:
        if not normal.done():
            normal.cancel()
            await asyncio.gather(normal, return_exceptions=True)


async def test_a_paused_guest_resumes_before_receiving_the_power_request(service, running_vm):
    await running_vm.qmp.execute("stop")
    running_vm.view.state = "PAUSED"
    await service.manager.graceful_stop("desktop")
    commands = [row["command"] for row in read_command_log(running_vm) if "command" in row]
    assert commands[commands.index("stop") + 1 :][:2] == ["cont", "system_powerdown"]
    assert running_vm.state is VmState.STOPPED


@pytest.mark.parametrize("action", ["restart", "reset"])
async def test_force_cancels_the_pending_rebuild_without_starting_a_replacement(service, action):
    desktop = DesktopManager(service)
    definition = await desktop.create(
        {
            "name": "Interrupted rebuild",
            "profile_id": "default",
            "source": "image",
            "path": service.config.profile("default").images.base,
            "image_format": "qcow2",
            "vcpus": 1,
            "memory_mib": 512,
            "disk_gib": 24,
            "network": "offline",
        }
    )
    vm_id = definition["vm_id"]
    await desktop.start(vm_id)
    instance = service.manager.get(vm_id)
    budget(instance, 60)
    write_behavior(instance, {"ignore_powerdown": True})
    disk = instance.paths.overlay.read_bytes()
    owner = desktop.get(vm_id).storage_owner
    rebuild = asyncio.create_task(getattr(desktop, action)(vm_id))
    try:
        await stopping(instance)
        await asyncio.wait_for(
            desktop.stop(vm_id, force=True, expected_instance_id=instance.view.instance_id), 4
        )
        with pytest.raises(PaletteError) as cancelled:
            await rebuild
        assert cancelled.value.code is ErrorCode.STATE_CONFLICT
        assert service.manager.get(vm_id) is instance
        assert instance.state is VmState.STOPPED
        assert instance.paths.overlay.read_bytes() == disk
        assert desktop.get(vm_id).storage_owner == owner
    finally:
        if not rebuild.done():
            rebuild.cancel()
            await asyncio.gather(rebuild, return_exceptions=True)


async def test_force_cancels_a_transient_reset(service, running_vm):
    budget(running_vm, 60)
    write_behavior(running_vm, {"ignore_powerdown": True})
    reset = asyncio.create_task(service.manager.reset("desktop"))
    try:
        await stopping(running_vm)
        await asyncio.wait_for(service.manager.stop("desktop", force=True), 4)
        with pytest.raises(PaletteError) as cancelled:
            await reset
        assert cancelled.value.code is ErrorCode.STATE_CONFLICT
        assert service.manager.get("desktop") is running_vm
        assert running_vm.state is VmState.STOPPED
    finally:
        if not reset.done():
            reset.cancel()
            await asyncio.gather(reset, return_exceptions=True)


async def test_a_resumed_guest_timeout_keeps_the_actual_running_state(service, running_vm):
    budget(running_vm, 0.15)
    write_behavior(running_vm, {"ignore_powerdown": True})
    await running_vm.qmp.execute("stop")
    running_vm.view.state = "PAUSED"
    with pytest.raises(PaletteError) as failed:
        await service.manager.graceful_stop("desktop")
    assert failed.value.code is ErrorCode.TIMEOUT
    assert running_vm.state is VmState.RUNNING
    assert running_vm.process.returncode is None


async def test_the_budget_includes_a_hung_power_command(service, running_vm, monkeypatch):
    budget(running_vm, 0.15)
    original = running_vm.qmp.execute

    async def hung(command, *args, **kwargs):
        if command == "system_powerdown":
            await asyncio.Event().wait()
        return await original(command, *args, **kwargs)

    monkeypatch.setattr(running_vm.qmp, "execute", hung)
    with pytest.raises(PaletteError) as failed:
        await asyncio.wait_for(service.manager.graceful_stop("desktop"), 1)
    assert failed.value.code is ErrorCode.TIMEOUT
    assert failed.value.details["operation"] == "guest-shutdown"
    assert running_vm.state is VmState.RUNNING
    assert running_vm.process.returncode is None


async def test_an_agent_lock_cannot_make_shutdown_wait_forever(service, running_vm):
    budget(running_vm, 0.15)
    agent = QemuGuestAgent("test-only")
    agent.online = True
    agent.commands = {"guest-shutdown"}
    running_vm.qemu_guest_agent = agent
    await agent.lock.acquire()
    try:
        with pytest.raises(PaletteError) as failed:
            await asyncio.wait_for(service.manager.graceful_stop("desktop"), 1)
        assert failed.value.code is ErrorCode.TIMEOUT
        assert running_vm.process.returncode is None
        assert not any(
            row.get("command") == "system_powerdown" for row in read_command_log(running_vm)
        )
    finally:
        agent.lock.release()


async def test_control_file_cleanup_is_bounded_while_another_transfer_holds_the_lock():
    agent = QemuGuestAgent("test-only", timeout_seconds=0.05)
    agent.online = True
    agent.transfers["old-owner"] = (1, "w", 0)
    await agent.lock.acquire()
    try:
        await asyncio.wait_for(agent.close_files(), 0.5)
        assert not agent.online
        assert not agent.transfers
    finally:
        agent.lock.release()


async def test_guest_agent_can_shutdown_even_when_qmp_is_unavailable(service, running_vm):
    qmp = running_vm.qmp
    running_vm.qmp = None
    agent = QemuGuestAgent("test-only")
    agent.online = True
    agent.commands = {"guest-shutdown"}

    async def guest_exit():
        running_vm.process.kill()

    agent.shutdown = AsyncMock(side_effect=guest_exit)
    running_vm.qemu_guest_agent = agent
    try:
        await service.manager.graceful_stop("desktop")
        agent.shutdown.assert_awaited_once()
        assert running_vm.state is VmState.STOPPED
    finally:
        await qmp.close()


async def test_failed_but_live_processes_remain_visible_and_can_be_stopped(service, running_vm):
    desktop = DesktopManager(service)
    running_vm.view.state = "FAILED"
    running_vm.qemu_guest_agent = None
    await running_vm.qmp.close()
    listed = await desktop.list()
    assert listed[0]["status"]["process_running"]
    with pytest.raises(PaletteError) as failed:
        await desktop.stop("desktop")
    assert failed.value.details["operation"] == "guest-shutdown"
    assert running_vm.state is VmState.FAILED
    await desktop.stop("desktop", force=True, expected_instance_id=running_vm.view.instance_id)
    assert running_vm.process.returncode is not None
    assert not (await desktop.list())[0]["status"]["process_running"]


async def test_an_enhancement_close_timeout_does_not_trap_a_stopped_guest(service, running_vm):
    running_vm.profile = running_vm.profile.model_copy(
        update={
            "timeouts": running_vm.profile.timeouts.model_copy(update={"qmp_command_seconds": 0.05})
        }
    )

    async def hang():
        await asyncio.Event().wait()

    running_vm.guest_tools.close = AsyncMock(side_effect=hang)
    await asyncio.wait_for(service.manager.stop("desktop", force=True), 2)
    assert running_vm.process.returncode is not None
    assert running_vm.state is VmState.STOPPED
