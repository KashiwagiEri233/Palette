"""The original disk participates in ordinary drive editing and deletion."""

from pathlib import Path

import pytest
from test_utm_drives import blank, save, setup_vm

from palette.desktop import DesktopManager
from palette.errors import PaletteError
from palette.primary_drive import PRIMARY_DRIVE_ID


async def test_original_disk_edits_use_the_common_drive_launch_path(service):
    desktop, vm_id = await setup_vm(service)
    data = await desktop.get_settings(vm_id)
    primary = data["settings"]["drives"][0]
    assert primary["id"] == PRIMARY_DRIVE_ID
    assert data["managed_disks"][PRIMARY_DRIVE_ID]["allocated"] is False
    primary.update(interface="ide", read_only=True, size_gib=32)
    await save(desktop, vm_id, [primary])
    restored = DesktopManager(service)
    data = await restored.get_settings(vm_id)
    assert data["draft"]["disk_gib"] == 32
    assert data["settings"]["drives"][0]["read_only"] is True
    await restored.start(vm_id)
    instance = service.manager.get(vm_id)
    assert len(instance.overrides.volumes) == 1
    assert instance.overrides.volumes[0].path == str(instance.paths.overlay)
    assert instance.overrides.volumes[0].read_only
    assert any(value.startswith("ide-hd,") for value in instance.plan.argv)
    assert not any(value.startswith("virtio-blk-pci,") for value in instance.plan.argv)
    await restored.stop(vm_id, force=True)


async def test_delete_unallocated_original_disk_never_recreates_it(service):
    desktop, vm_id = await setup_vm(service)
    source = Path(desktop.get(vm_id).draft.path)
    before = source.read_bytes()
    await save(desktop, vm_id, [])
    restored = DesktopManager(service)
    assert (await restored.get_settings(vm_id))["settings"]["drives"] == []
    for action in (restored.start, restored.reset):
        await action(vm_id)
        instance = service.manager.get(vm_id)
        assert not instance.paths.overlay.exists()
        assert not instance.overrides.volumes
        assert not any(value.startswith("virtio-blk-pci,") for value in instance.plan.argv)
        await restored.stop(vm_id, force=True)
    assert source.read_bytes() == before


async def test_delete_allocated_original_keeps_other_disks_and_supports_their_resize(service):
    desktop, vm_id = await setup_vm(service)
    primary = (await desktop.get_settings(vm_id))["settings"]["drives"][0]
    await save(desktop, vm_id, [blank(), primary])
    await desktop.start(vm_id)
    instance = service.manager.get(vm_id)
    initial_path = instance.paths.overlay
    initial_owner = desktop.get(vm_id).storage_owner
    assert instance.overrides.volumes[0].device_id == "drive-data"
    await desktop.stop(vm_id, force=True)
    data = await desktop.get_settings(vm_id)
    data_path = Path(data["managed_disks"]["drive-data"]["path"])
    remaining = [drive for drive in data["settings"]["drives"] if drive["id"] != PRIMARY_DRIVE_ID]
    # Merely editing the draft retains the original file.
    assert initial_path.is_file()
    await save(desktop, vm_id, remaining)
    assert not initial_path.exists() and data_path.exists()
    # Its owner also holds persistent UEFI variables; retain that owner directory.
    assert desktop.get(vm_id).storage_owner == initial_owner
    current = await desktop.get_settings(vm_id)
    await desktop.resize_disk(
        vm_id,
        {
            "revision": current["revision"],
            "drive_id": "drive-data",
            "disk_gib": 2,
        },
    )
    restored = DesktopManager(service)
    current = await restored.get_settings(vm_id)
    assert current["settings"]["drives"][0]["size_gib"] == 2
    assert current["managed_disks"]["drive-data"]["virtual_bytes"] == 2 * 1024**3
    await save(restored, vm_id, current["settings"]["drives"])
    await restored.start(vm_id)
    assert [volume.device_id for volume in service.manager.get(vm_id).overrides.volumes] == [
        "drive-data"
    ]
    await restored.stop(vm_id, force=True)
    assert not initial_path.exists()
    with pytest.raises(PaletteError):
        await restored.resize_disk(
            vm_id, {"revision": restored.get(vm_id).revision, "disk_gib": 64}
        )


async def test_primary_disk_cannot_be_deleted_while_another_drive_references_it(service):
    desktop, vm_id = await setup_vm(service)
    await desktop.start(vm_id)
    path = service.manager.get(vm_id).paths.overlay
    await desktop.stop(vm_id, force=True)
    data = await desktop.get_settings(vm_id)
    primary = data["settings"]["drives"][0]
    primary["read_only"] = True
    other = {
        "id": "drive-alias",
        "media": "disk",
        "path": str(path),
        "format": "qcow2",
        "read_only": True,
    }
    await save(desktop, vm_id, [primary, other])
    with pytest.raises(PaletteError, match="引用"):
        await save(desktop, vm_id, [other])
    assert path.exists()
    assert (await desktop.get_settings(vm_id))["settings"]["drives"][0]["id"] == PRIMARY_DRIVE_ID


async def test_primary_cleanup_failure_survives_reset_and_can_be_retried(service, monkeypatch):
    desktop, vm_id = await setup_vm(service)
    await desktop.start(vm_id)
    path = service.manager.get(vm_id).paths.overlay
    await desktop.stop(vm_id, force=True)
    unlink = Path.unlink

    def locked(target, *args, **kwargs):
        if target == path:
            raise PermissionError("locked")
        return unlink(target, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", locked)
        reply = await save(desktop, vm_id, [])
    assert reply["pending_disk_deletions"] == [PRIMARY_DRIVE_ID]
    await desktop.reset(vm_id)
    await desktop.stop(vm_id, force=True)
    restored = DesktopManager(service)
    await save(restored, vm_id, [], delete_drives=[PRIMARY_DRIVE_ID])
    assert not path.exists()
    assert not restored.get(vm_id).data_disks


@pytest.mark.parametrize("image_format", ["raw", "qcow2"])
async def test_added_disk_resize_journal_recovers_definition_write_failure(
    service, monkeypatch, image_format
):
    desktop, vm_id = await setup_vm(service)
    await save(desktop, vm_id, [blank(image_format=image_format)])
    original = desktop.repository.save

    def fail(_):
        raise OSError("injected save failure")

    monkeypatch.setattr(desktop.repository, "save", fail)
    with pytest.raises(OSError, match="injected"):
        await desktop.resize_disk(
            vm_id,
            {
                "revision": desktop.get(vm_id).revision,
                "drive_id": "drive-data",
                "disk_gib": 2,
            },
        )
    monkeypatch.setattr(desktop.repository, "save", original)
    restored = DesktopManager(service)
    await restored.recover_operations()
    record = restored.get(vm_id).data_disks["drive-data"]
    assert record.size_gib == 2 and record.virtual_size == 2 * 1024**3
    assert not restored.storage.operation_path(vm_id).exists()
    await restored.start(vm_id)
    await restored.stop(vm_id, force=True)
