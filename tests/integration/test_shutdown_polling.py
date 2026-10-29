"""Desktop status requests may outlive the VM they were polling."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from palette.errors import ErrorCode, PaletteError
from palette.operator import OperatorClient
from palette.operator.protocol import DesktopCommand
from palette.runtime import RuntimeOwner


async def test_late_console_polls_after_shutdown_keep_mutations_fenced(service):
    owner = RuntimeOwner(service)
    await owner.start()
    try:
        client = OperatorClient(service.config.runtime_root)
        await service.manager.start("desktop", "default")
        instance_id = service.manager.get("desktop").view.instance_id
        lease = await client.call(
            "control.acquire", vm_id="desktop", payload={"instance_id": instance_id}
        )
        authority = {"instance_id": instance_id, "lease_id": lease["lease_id"]}
        await service.manager.stop("desktop", force=True)

        status = await client.call(
            "guest.status", vm_id="desktop", payload={"instance_id": instance_id}
        )
        assert status["qga"]["online"] is False
        assert status["qga"]["commands"] == []
        assert status["desktop"]["clipboard"] is False
        assert status["desktop"]["resize"] is False
        assert status["shares"]["available"] is False
        display = await client.call("display.sync", vm_id="desktop")
        assert display["status"]["state"] == "STOPPED"
        assert await client.call(
            "control.release", vm_id="desktop", payload={"lease_id": lease["lease_id"]}
        ) == {"release_confirmed": True}

        with pytest.raises(PaletteError) as mutation:
            await client.call("guest.sync_time", vm_id="desktop", payload=authority)
        assert mutation.value.code == ErrorCode.VM_NOT_RUNNING

        await service.manager.start("desktop", "default")
        with pytest.raises(PaletteError) as stale:
            await client.call("guest.status", vm_id="desktop", payload={"instance_id": instance_id})
        assert stale.value.code == ErrorCode.STALE_FRAME
        with pytest.raises(PaletteError) as missing:
            await client.call("display.sync", vm_id="missing")
        assert missing.value.code == ErrorCode.VM_NOT_FOUND
    finally:
        await owner.close()


async def test_display_sync_preserves_capture_failures(service, running_vm, monkeypatch):
    owner = RuntimeOwner(service)
    failure = PaletteError(ErrorCode.QMP_UNAVAILABLE, "capture channel failed")
    monkeypatch.setattr(service, "observe", AsyncMock(side_effect=failure))
    with pytest.raises(PaletteError) as capture:
        await owner.route(DesktopCommand(method="display.sync", vm_id="desktop"))
    assert capture.value is failure


async def test_live_guest_still_requires_confirmed_input_release(service, running_vm, monkeypatch):
    lease = await service.control.acquire("desktop", running_vm.view.instance_id)
    monkeypatch.setattr(
        running_vm.controller,
        "emergency_release",
        AsyncMock(return_value=SimpleNamespace(release_confirmed=False)),
    )
    assert await service.control.release("desktop", lease["lease_id"]) == {
        "release_confirmed": False
    }
    assert running_vm.process.returncode is None
    assert service.control.leases["desktop"].state == "faulted"
