"""Desktop hardware reboot preserves the instance and retires its input credentials."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from conftest import read_command_log, write_behavior
from palette.desktop import DesktopManager
from palette.errors import ErrorCode, PaletteError
from palette.operator import OperatorClient
from palette.operator.protocol import DesktopCommand
from palette.runtime import RuntimeOwner


async def create_vm(desktop, service):
    definition = await desktop.create(
        {
            "name": "Hardware reboot",
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
    await desktop.start(definition["vm_id"])
    return service.manager.get(definition["vm_id"])


@pytest.mark.parametrize("kind", ["daemon", "embedded"])
async def test_hardware_reboot_over_ipc_preserves_process_storage_and_retires_control(
    service, kind
):
    owner = RuntimeOwner(service, kind=kind)
    await owner.start()
    try:
        client = OperatorClient(service.config.runtime_root)
        assert "vm-reboot-v1" in (await client.call("hello"))["features"]
        instance = await create_vm(owner.desktop, service)
        vm_id = instance.view.vm_id
        write_behavior(instance, {"ignore_powerdown": True})
        process = instance.process
        disk = instance.paths.overlay
        disk_before = disk.read_bytes()
        storage_owner = owner.desktop.get(vm_id).storage_owner
        epoch = instance.view.boot_epoch
        observed = await service.observe(vm_id, actor="operator")
        assert observed.frame is not None
        lease = await service.control.acquire(vm_id, instance.view.instance_id)
        await service.control.input(
            vm_id,
            lease["lease_id"],
            1,
            lease["display_epoch"],
            observed.frame.native_size.width,
            observed.frame.native_size.height,
            [{"type": "key", "key": "a", "down": True}],
        )
        observed = await service.observe(vm_id, actor="operator")
        assert observed.frame is not None and instance.controller is not None
        assert instance.controller.frame_by_id(observed.frame.frame_id) is observed.frame
        command_offset = len(read_command_log(instance))
        result = await client.call(
            "vms.reboot", vm_id=vm_id, payload={"expected_instance_id": instance.view.instance_id}
        )
        assert result == {
            "vm_id": vm_id,
            "instance_id": instance.view.instance_id,
            "state": "RUNNING",
        }
        async with asyncio.timeout(3):
            while instance.view.boot_epoch == epoch:  # noqa: ASYNC110 - observe QMP RESET
                await asyncio.sleep(0.01)
        assert service.manager.get(vm_id) is instance
        assert instance.process is process and process.returncode is None
        assert instance.paths.overlay == disk and disk.read_bytes() == disk_before
        assert owner.desktop.get(vm_id).storage_owner == storage_owner
        assert instance.view.boot_epoch == epoch + 1
        assert vm_id not in service.control.leases
        assert instance.controller is not None
        assert instance.controller.ledger_snapshot()["keys"] == []
        with pytest.raises(PaletteError) as old_lease:
            service.control.heartbeat(vm_id, lease["lease_id"])
        assert old_lease.value.code is ErrorCode.STALE_FRAME
        with pytest.raises(PaletteError) as old_frame:
            instance.controller.frame_by_id(observed.frame.frame_id)
        assert old_frame.value.code is ErrorCode.STALE_FRAME
        commands = [
            row["command"]
            for row in read_command_log(instance)[command_offset:]
            if "command" in row
        ]
        assert commands.count("system_reset") == 1
        assert not {"system_powerdown", "quit"}.intersection(commands)
    finally:
        await owner.close()


@pytest.mark.parametrize("state", ["STOPPED", "PAUSED", "STOPPING", "FAILED"])
async def test_reboot_rejects_nonrunning_vm(service, running_vm, state):
    desktop = DesktopManager(service)
    running_vm.view.state = state
    try:
        with pytest.raises(PaletteError) as error:
            await desktop.reboot("desktop", expected_instance_id=running_vm.view.instance_id)
        assert error.value.code is ErrorCode.VM_NOT_RUNNING
        assert "system_reset" not in [row.get("command") for row in read_command_log(running_vm)]
    finally:
        running_vm.view.state = "RUNNING"


@pytest.mark.parametrize(
    "payload", [{}, {"expected_instance_id": None}, {"expected_instance_id": ""}]
)
async def test_reboot_requires_an_explicit_instance(service, running_vm, payload):
    owner = RuntimeOwner(service)
    with pytest.raises(PaletteError) as error:
        await owner.route(DesktopCommand(method="vms.reboot", vm_id="desktop", payload=payload))
    assert error.value.code is ErrorCode.INVALID_ARGUMENT
    assert "system_reset" not in [row.get("command") for row in read_command_log(running_vm)]


async def test_queued_reboot_cannot_target_a_replacement_instance(service):
    desktop = DesktopManager(service)
    instance = await create_vm(desktop, service)
    vm_id, old_id = instance.view.vm_id, instance.view.instance_id
    async with service.manager.operations.hold(vm_id):
        pending = asyncio.create_task(desktop.reboot(vm_id, expected_instance_id=old_id))
        await asyncio.sleep(0)
        await desktop.stop(vm_id)
        await desktop.start(vm_id)
    with pytest.raises(PaletteError) as stale:
        await pending
    assert stale.value.code is ErrorCode.STALE_FRAME
    replacement = service.manager.get(vm_id)
    assert replacement.view.instance_id != old_id
    assert "system_reset" not in [row.get("command") for row in read_command_log(replacement)]


@pytest.mark.parametrize("queued", [False, True])
async def test_reboot_checks_runtime_admission_after_waiting_for_the_lock(
    service, running_vm, queued
):
    desktop = DesktopManager(service)
    try:
        async with service.manager.operations.hold("desktop"):
            if not queued:
                service.manager.operations.closing = True
            pending = asyncio.create_task(
                desktop.reboot("desktop", expected_instance_id=running_vm.view.instance_id)
            )
            await asyncio.sleep(0)
            service.manager.operations.closing = True
        with pytest.raises(PaletteError) as closed:
            await pending
        assert closed.value.code is ErrorCode.STATE_CONFLICT
        assert "system_reset" not in [row.get("command") for row in read_command_log(running_vm)]
    finally:
        service.manager.operations.closing = False


async def test_reboot_propagates_qmp_failure_without_restarting_or_stopping(
    service, running_vm, monkeypatch
):
    desktop = DesktopManager(service)
    execute = running_vm.qmp.execute
    epoch = running_vm.view.boot_epoch

    async def fail_reset(command, *args, **kwargs):
        if command == "system_reset":
            raise PaletteError(ErrorCode.QMP_UNAVAILABLE, "reset command failed")
        return await execute(command, *args, **kwargs)

    monkeypatch.setattr(running_vm.qmp, "execute", fail_reset)
    with pytest.raises(PaletteError, match="reset command failed"):
        await desktop.reboot("desktop", expected_instance_id=running_vm.view.instance_id)
    assert running_vm.process.returncode is None
    assert running_vm.view.boot_epoch == epoch
    assert not {"system_powerdown", "quit"}.intersection(
        row.get("command") for row in read_command_log(running_vm)
    )


async def test_reboot_requires_confirmed_input_release(service, running_vm, monkeypatch):
    desktop = DesktopManager(service)
    monkeypatch.setattr(service.control, "revoke", AsyncMock(return_value=False))
    with pytest.raises(PaletteError, match="input release was not confirmed"):
        await desktop.reboot("desktop", expected_instance_id=running_vm.view.instance_id)
    assert "system_reset" not in [row.get("command") for row in read_command_log(running_vm)]
