"""UTM-style creation/import, ordering, and deletion at settings save."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from palette.desktop import DesktopManager
from palette.errors import PaletteError
from palette.managed_drives import managed_disk_path


async def setup_vm(service):
    desktop = DesktopManager(service)
    vm = await desktop.create(
        {
            "name": "UTM drives",
            "profile_id": "default",
            "source": "image",
            "path": service.config.profile("default").images.base,
            "vcpus": 1,
            "memory_mib": 512,
            "disk_gib": 24,
            "network": "offline",
        }
    )
    return desktop, vm["vm_id"]


async def save(desktop, vm_id, drives, **overrides):
    data = await desktop.get_settings(vm_id)
    return await desktop.update(
        vm_id,
        {
            "revision": data["revision"],
            "name": data["draft"]["name"],
            "vcpus": 1,
            "memory_mib": 512,
            "network": "offline",
            "iso_attached": False,
            "settings": {**data["settings"], "drives": drives},
            **overrides,
        },
    )


def blank(drive_id="drive-data", interface="virtio", image_format="qcow2"):
    return {
        "id": drive_id,
        "media": "disk",
        "size_gib": 1,
        "format": image_format,
        "interface": interface,
        "read_only": False,
    }


@pytest.mark.parametrize("image_format", ["raw", "qcow2"])
async def test_managed_import_flattens_backing_chain_and_preserves_source(
    service, tmp_path, image_format
):
    desktop, vm_id = await setup_vm(service)
    source = tmp_path / "导入.qcow2"
    source.write_bytes(
        b"QFI\xfb"
        + json.dumps(
            {
                "driver": "qcow2",
                "virtual_size": 1536 * 1024**2,
                "backing": service.config.profile("default").images.base,
                "backing_format": "qcow2",
            }
        ).encode()
    )
    original = source.read_bytes()
    await save(
        desktop,
        vm_id,
        [
            {
                "id": "drive-copy",
                "media": "disk",
                "path": str(source),
                "format": image_format,
                "import_copy": True,
                "read_only": False,
            }
        ],
    )
    restored = DesktopManager(service)
    definition = restored.get(vm_id)
    drive = restored.settings(definition).drives[0]
    record = definition.data_disks[drive.id]
    path = managed_disk_path(restored, vm_id, record)
    assert drive.path is None and not drive.import_copy and not drive.read_only
    assert record.virtual_size == 1536 * 1024**2 and record.format == image_format
    info = await service.manager.images_for(definition.profile).inspect_image(path)
    assert info.chain == (path,) and info.format == image_format
    assert source.read_bytes() == original
    await restored.start(vm_id)
    await restored.stop(vm_id, force=True)
    await save(restored, vm_id, [])
    assert not path.exists() and source.read_bytes() == original


async def test_interfaces_order_and_save_time_deletion(service):
    desktop, vm_id = await setup_vm(service)
    interfaces = ["virtio", "ide", "scsi", "nvme", "usb"]
    await save(
        desktop,
        vm_id,
        [blank(f"drive-{kind}", kind, "raw" if kind == "ide" else "qcow2") for kind in interfaces],
    )
    data = await desktop.get_settings(vm_id)
    path = Path(data["managed_disks"]["drive-ide"]["path"])
    assert path.name == "disk.raw"
    reordered = list(reversed(data["settings"]["drives"]))
    await save(desktop, vm_id, reordered)
    restored = DesktopManager(service)
    assert [drive.id for drive in restored.settings(restored.get(vm_id)).drives] == [
        drive["id"] for drive in reordered
    ]
    await restored.start(vm_id)
    argv = service.manager.find(vm_id).plan.argv
    for model in ("virtio-blk-pci", "ide-hd", "scsi-hd", "nvme", "usb-bot"):
        assert any(value.startswith(model + ",") for value in argv)
    await restored.stop(vm_id, force=True)
    # Editing/removing in memory does not delete; saving does.
    remaining = [drive for drive in reordered if drive["id"] != "drive-ide"]
    assert path.exists()
    await save(restored, vm_id, remaining)
    assert not path.exists()


async def test_failed_validation_and_conversion_keep_disks_and_sources(
    service, tmp_path, monkeypatch
):
    from palette import managed_drives

    desktop, vm_id = await setup_vm(service)
    await save(desktop, vm_id, [blank()])
    data = await desktop.get_settings(vm_id)
    path = Path(data["managed_disks"]["drive-data"]["path"])
    source = tmp_path / "copy.raw"
    source.write_bytes(bytes(4096))
    with pytest.raises(PaletteError):
        await save(desktop, vm_id, [], vcpus=0)
    assert path.exists()

    async def failed_convert(*args, **kwargs):
        return 1, "", "conversion failed"

    monkeypatch.setattr(managed_drives, "run_tool", failed_convert)
    with pytest.raises(PaletteError):
        await save(
            desktop,
            vm_id,
            [{"id": "drive-new", "media": "disk", "path": str(source), "import_copy": True}],
        )
    assert path.exists() and source.read_bytes() == bytes(4096)
    assert set(desktop.get(vm_id).data_disks) == {"drive-data"}


async def test_cleanup_failure_is_persisted_and_retryable(service, monkeypatch):
    desktop, vm_id = await setup_vm(service)
    await save(desktop, vm_id, [blank()])
    data = await desktop.get_settings(vm_id)
    path = Path(data["managed_disks"]["drive-data"]["path"])
    unlink = Path.unlink

    def fail_file(target, *args, **kwargs):
        if target == path:
            raise PermissionError("locked")
        return unlink(target, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail_file)
        reply = await save(desktop, vm_id, [])
    assert reply["pending_disk_deletions"] == ["drive-data"]
    assert path.exists() and desktop.get(vm_id).data_disks["drive-data"].deleting
    restored = DesktopManager(service)
    assert not restored.settings(restored.get(vm_id)).drives
    await save(restored, vm_id, [], delete_drives=["drive-data"])
    assert not path.exists() and not restored.get(vm_id).data_disks


async def test_reference_prevents_save_time_delete(service):
    desktop, vm_id = await setup_vm(service)
    await save(desktop, vm_id, [blank()])
    data = await desktop.get_settings(vm_id)
    path = Path(data["managed_disks"]["drive-data"]["path"])
    other = await desktop.create(
        {
            **desktop.get(vm_id).draft.model_dump(),
            "settings": None,
            "path": str(path),
            "disk_gib": 1,
        }
    )
    with pytest.raises(PaletteError, match="引用"):
        await save(desktop, vm_id, [])
    assert path.exists() and desktop.get(vm_id).revision == data["revision"]
    await desktop.delete(other["vm_id"])
    await save(desktop, vm_id, [])
    assert not path.exists()
