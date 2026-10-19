"""Enhancement degradation preserves baseline input and existing ownership policy."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from palette.config import GuestDesktopConfig, QemuGuestAgentConfig, SharedFolderConfig
from palette.definitions import DefinitionRepository
from palette.desktop import DesktopManager
from palette.enhancements import EnhancementRoutes
from palette.errors import ErrorCode, PaletteError
from palette.guest_agent import QemuGuestAgent


async def test_offline_guest_keeps_baseline_and_disables_individual_features(service, running_vm):
    routes = EnhancementRoutes(service)
    payload = {"instance_id": running_vm.view.instance_id}
    status = await routes.route("desktop", "guest.status", payload)
    assert not status["qga"]["online"]
    assert not status["desktop"]["clipboard"]
    assert not status["desktop"]["resize"]
    assert not status["shares"]["available"]
    lease = await service.control.acquire("desktop", running_vm.view.instance_id)
    with pytest.raises(PaletteError) as failure:
        await routes.route(
            "desktop",
            "control.clipboard.set",
            {**payload, "lease_id": lease["lease_id"], "text": "中文"},
        )
    assert failure.value.code == ErrorCode.GUEST_TOOL_UNAVAILABLE
    # A failed enhancement must neither replay as keyboard input nor destroy its lease.
    await service.control.input(
        "desktop",
        lease["lease_id"],
        1,
        running_vm.view.display_epoch,
        320,
        200,
        [{"type": "key", "key": "a", "down": True}, {"type": "key", "key": "a", "down": False}],
    )
    assert (await service.observe("desktop", actor="operator")).ok


async def test_enhancements_reject_missing_stale_and_other_instance_authority(service, running_vm):
    routes = EnhancementRoutes(service)
    with pytest.raises(PaletteError) as failure:
        await routes.route(
            "desktop", "guest.sync_time", {"instance_id": running_vm.view.instance_id}
        )
    assert failure.value.code == ErrorCode.CONTROL_NOT_OWNED
    lease = await service.control.acquire("desktop", running_vm.view.instance_id)
    with pytest.raises(PaletteError):
        await routes.route(
            "desktop",
            "guest.sync_time",
            {"instance_id": "different-instance", "lease_id": lease["lease_id"]},
        )
    await service.control.release("desktop", lease["lease_id"])
    with pytest.raises(PaletteError):
        await routes.route(
            "desktop",
            "guest.sync_time",
            {"instance_id": running_vm.view.instance_id, "lease_id": lease["lease_id"]},
        )


async def test_releasing_control_cleans_guest_file_handles(service, running_vm):
    agent = QemuGuestAgent("test-only")
    agent.close_files = AsyncMock()
    agent.close = AsyncMock()
    running_vm.qemu_guest_agent = agent
    lease = await service.control.acquire("desktop", running_vm.view.instance_id)
    await service.control.release("desktop", lease["lease_id"])
    agent.close_files.assert_awaited_once()


async def test_unknown_guest_shutdown_does_not_fall_back_or_kill(service, running_vm):
    agent = QemuGuestAgent("test-only")
    agent.online = True
    agent.commands = {"guest-shutdown"}
    agent.shutdown = AsyncMock(
        side_effect=PaletteError(ErrorCode.ACTION_OUTCOME_UNKNOWN, "unknown")
    )
    agent.close = AsyncMock()
    running_vm.qemu_guest_agent = agent
    with pytest.raises(PaletteError) as failure:
        await service.manager.graceful_stop(
            "desktop", expected_instance_id=running_vm.view.instance_id
        )
    assert failure.value.code == ErrorCode.ACTION_OUTCOME_UNKNOWN
    assert running_vm.state == "RUNNING"
    assert running_vm.process.returncode is None


async def test_old_definitions_inherit_only_missing_channels_and_preserve_optouts(
    service, tmp_path
):
    desktop = DesktopManager(service)
    original = service.config.profile("default")
    created = await desktop.create(
        {
            "name": "Migration",
            "profile_id": "default",
            "source": "image",
            "path": original.images.base,
            "vcpus": 1,
            "memory_mib": 512,
            "disk_gib": 24,
            "network": "offline",
        }
    )
    vm_id = created["vm_id"]
    path = desktop.directory / f"{vm_id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["profile"].pop("guest_agent")
    data["profile"].pop("guest_desktop")
    path.write_text(json.dumps(data), encoding="utf-8")
    service.config.profiles["default"] = original.model_copy(
        update={
            "guest_agent": QemuGuestAgentConfig(enabled=True),
            "guest_desktop": GuestDesktopConfig(clipboard=True, resize=True, shared_folders=True),
            "shared_folders": [SharedFolderConfig(tag="not-inherited", path=str(tmp_path))],
        }
    )
    migrated = DefinitionRepository(service.manager).get(vm_id).profile
    assert migrated.guest_agent.enabled and migrated.guest_desktop.resize
    assert not migrated.shared_folders
    assert migrated.resources.memory_mib == 512
    assert migrated.devices == original.devices
    data["profile"]["guest_agent"] = {"enabled": False}
    path.write_text(json.dumps(data), encoding="utf-8")
    assert not DefinitionRepository(service.manager).get(vm_id).profile.guest_agent.enabled
