"""Persistent additional disks/optical drives preserve system media and stable IDs."""

from __future__ import annotations

from pathlib import Path

import pytest

from palette.desktop import DesktopManager
from palette.errors import ErrorCode, PaletteError


async def fixture_vm(service, tmp_path):
    desktop = DesktopManager(service)
    installer = tmp_path / "system.iso"
    installer.write_bytes(b"installation media")
    document = await desktop.create(
        {
            "name": "Multiple drives",
            "profile_id": "default",
            "source": "iso",
            "path": str(installer),
            "vcpus": 1,
            "memory_mib": 512,
            "disk_gib": 24,
            "network": "offline",
        }
    )
    return desktop, document["vm_id"], installer


def update_payload(result, drives):
    return {
        "revision": result["revision"],
        "name": result["draft"]["name"],
        "vcpus": 1,
        "memory_mib": 512,
        "network": "offline",
        "iso_attached": True,
        "settings": {**result["settings"], "drives": drives},
    }


async def test_multiple_drives_save_reload_start_and_remove_without_deleting_files(
    service, tmp_path
):
    desktop, vm_id, installer = await fixture_vm(service, tmp_path)
    settings = await desktop.get_settings(vm_id)
    disk = service.config.profile("default").images.base
    drives = [
        {"id": "drive-data", "media": "disk", "path": disk, "format": "qcow2"},
        {"id": "drive-disc", "media": "cdrom", "path": str(installer)},
        {"id": "drive-empty", "media": "cdrom"},
    ]
    await desktop.update(vm_id, update_payload(settings, drives))
    restored = DesktopManager(service)
    for _ in range(2):
        await restored.start(vm_id)
        instance = service.manager.find(vm_id)
        assert "virtio-blk-pci,drive=drive-data,id=drive-data" in instance.plan.argv
        assert "scsi-cd,id=drive-disc,drive=drive-disc,bus=drive-disc-usb.0" in instance.plan.argv
        assert "scsi-cd,id=drive-empty,bus=drive-empty-usb.0" in instance.plan.argv
        assert len(instance.overrides.volumes) == 4  # system ISO + 3 additional drives
        assert restored.get(vm_id).iso_attached
        await restored.stop(vm_id, force=True)
    result = await restored.get_settings(vm_id)
    assert [item["id"] for item in result["settings"]["drives"]] == [item["id"] for item in drives]
    await restored.update(vm_id, update_payload(result, []))
    assert not restored.settings(restored.get(vm_id)).drives
    assert installer.read_bytes() == b"installation media"
    assert service.config.profile("default").devices.display_model == "VGA"


@pytest.mark.parametrize(
    "damage",
    [
        "duplicate-id",
        "writable-base",
        "missing",
        "writable-cd",
        "unsafe-id",
        "too-many",
        "wrong-format",
    ],
)
async def test_invalid_drives_do_not_save(service, tmp_path, damage):
    desktop, vm_id, _ = await fixture_vm(service, tmp_path)
    result = await desktop.get_settings(vm_id)
    disk = service.config.profile("default").images.base
    drives = [{"id": "drive-data", "media": "disk", "path": disk, "format": "qcow2"}]
    if damage == "duplicate-id":
        drives *= 2
    elif damage == "writable-base":
        drives[0]["read_only"] = False
    elif damage == "missing":
        drives[0]["path"] = str(tmp_path / "missing.qcow2")
    elif damage == "writable-cd":
        drives = [{"id": "drive-disc", "media": "cdrom", "read_only": False}]
    elif damage == "unsafe-id":
        drives[0]["id"] = "disk,bus=pci.0"
    elif damage == "too-many":
        drives = [{"id": f"drive-{index}", "media": "cdrom"} for index in range(9)]
    else:
        drives[0]["format"] = "raw"
    with pytest.raises(PaletteError):
        await desktop.update(vm_id, update_payload(result, drives))
    assert desktop.get(vm_id).revision == result["revision"]


async def test_duplicate_writable_disk_is_rejected_before_save(service, tmp_path):
    desktop, vm_id, _ = await fixture_vm(service, tmp_path)
    result = await desktop.get_settings(vm_id)
    disk = tmp_path / "data.qcow2"
    disk.write_bytes(Path(service.config.profile("default").images.base).read_bytes())
    drives = [
        {"id": name, "media": "disk", "path": str(disk), "format": "qcow2", "read_only": False}
        for name in ("drive-first", "drive-second")
    ]
    with pytest.raises(PaletteError, match="重复挂载"):
        await desktop.update(vm_id, update_payload(result, drives))
    assert desktop.get(vm_id).revision == result["revision"]


async def test_running_hardware_changes_are_rejected(service, tmp_path):
    desktop, vm_id, _ = await fixture_vm(service, tmp_path)
    result = await desktop.get_settings(vm_id)
    await desktop.start(vm_id)
    with pytest.raises(PaletteError) as error:
        await desktop.update(
            vm_id, update_payload(result, [{"id": "drive-empty", "media": "cdrom"}])
        )
    assert error.value.code == ErrorCode.STATE_CONFLICT
