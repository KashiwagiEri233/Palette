"""Blank data disks are allocated transactionally and retained independently of system storage."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from palette import managed_drives
from palette.desktop import DesktopManager
from palette.errors import ErrorCode, PaletteError
from palette.managed_disk_delete import delete_managed_disk
from palette.managed_drives import managed_drive_path


async def create_vm(service):
    desktop = DesktopManager(service)
    document = await desktop.create(
        {
            "name": "Blank disks",
            "profile_id": "default",
            "source": "image",
            "path": service.config.profile("default").images.base,
            "vcpus": 1,
            "memory_mib": 512,
            "disk_gib": 24,
            "network": "offline",
        }
    )
    return desktop, document["vm_id"]


async def save_drives(desktop, vm_id, drives, **settings):
    result = await desktop.get_settings(vm_id)
    return await desktop.update(
        vm_id,
        {
            "revision": result["revision"],
            "name": result["draft"]["name"],
            "vcpus": 1,
            "memory_mib": 512,
            "network": "offline",
            "iso_attached": False,
            "settings": {**result["settings"], "drives": drives, **settings},
        },
    )


def blank(name="drive-data", size=2):
    return {"id": name, "media": "disk", "size_gib": size}


def data_path(desktop, vm_id):
    definition = desktop.get(vm_id)
    return managed_drive_path(desktop, definition, desktop.settings(definition).drives[0])


async def test_blank_disk_reload_restart_reset_remove_and_delete(service):
    desktop, vm_id = await create_vm(service)
    assert not desktop.get(vm_id).data_disks
    await save_drives(desktop, vm_id, [blank()])
    path = data_path(desktop, vm_id)
    images = service.manager.images_for(desktop.get(vm_id).profile)
    info = await images.inspect_image(path)
    assert info.format == "qcow2" and info.virtual_size == 2 * 1024**3
    assert info.chain == (path,)
    # The fake image stores metadata; retain a content marker across lifecycle changes.
    metadata = json.loads(path.read_bytes()[4:])
    metadata["test_data"] = "retained data"
    path.write_bytes(b"QFI\xfb" + json.dumps(metadata).encode())
    original = path.read_bytes()
    restored = DesktopManager(service)
    assert data_path(restored, vm_id) == path
    assert (await restored.get_settings(vm_id))["managed_disks"]["drive-data"]["size_gib"] == 2
    for _ in range(2):
        await restored.start(vm_id)
        instance = service.manager.find(vm_id)
        assert instance.overrides.volumes[0].path == str(path)
        assert not instance.overrides.volumes[0].read_only
        await restored.stop(vm_id, force=True)
    await restored.reset(vm_id)
    await restored.stop(vm_id, force=True)
    assert path.read_bytes() == original
    await save_drives(restored, vm_id, [])
    service.manager.storage.prune(vm_id, restored.get(vm_id).profile, [], delete_all=True)
    assert not path.exists()
    await restored.delete(vm_id)
    assert not path.exists()


@pytest.mark.parametrize("damage", ["capacity", "external", "missing", "manifest"])
async def test_allocated_disks_cannot_be_silently_replaced(service, damage):
    desktop, vm_id = await create_vm(service)
    await save_drives(desktop, vm_id, [blank()])
    path = data_path(desktop, vm_id)
    drives = [blank()]
    if damage == "capacity":
        drives = [blank(size=3)]
    elif damage == "external":
        drives = [{"id": "drive-data", "media": "disk", "path": str(path), "format": "qcow2"}]
    elif damage == "missing":
        path.unlink()
    else:
        record = desktop.get(vm_id).data_disks["drive-data"]
        manifest = path.parents[1] / "manifest.json"
        assert record.owner == path.parents[1].name
        data = json.loads(manifest.read_text())
        data["data_drive_id"] = "drive-other"
        manifest.write_text(json.dumps(data), encoding="utf-8")
    revision = desktop.get(vm_id).revision
    with pytest.raises(PaletteError):
        await save_drives(desktop, vm_id, drives)
    assert desktop.get(vm_id).revision == revision
    if damage == "missing":
        assert not path.exists()


@pytest.mark.parametrize(
    "drive",
    [
        blank(size=0),
        blank(size=4097),
        blank(size=1.5),
        {**blank(), "format": "vmdk"},
        {**blank(), "path": "external.qcow2"},
        {**blank(), "media": "cdrom"},
    ],
)
async def test_invalid_blank_disks_create_no_files(service, drive):
    desktop, vm_id = await create_vm(service)
    with pytest.raises(PaletteError):
        await save_drives(desktop, vm_id, [drive])
    assert not list(desktop.guard.resolve("vms").glob(f"{vm_id}/data_*"))


@pytest.mark.parametrize("failure", ["create", "save", "second"])
async def test_transaction_failure_removes_only_new_disks(service, monkeypatch, failure):
    desktop, vm_id = await create_vm(service)
    await save_drives(desktop, vm_id, [blank()])
    original = data_path(desktop, vm_id)
    contents = original.read_bytes()
    run = managed_drives.run_tool
    calls = 0

    async def fail_create(*args, **kwargs):
        nonlocal calls
        calls += 1
        if failure == "create" or (failure == "second" and calls == 2):
            return 1, "", "failed"
        return await run(*args, **kwargs)

    def fail_save(_):
        raise OSError("definition save failed")

    monkeypatch.setattr(managed_drives, "run_tool", fail_create)
    if failure == "save":
        monkeypatch.setattr(desktop, "_save", fail_save)
    with pytest.raises((PaletteError, OSError)):
        await save_drives(desktop, vm_id, [blank(), blank("drive-new"), blank("drive-next")])
    assert original.read_bytes() == contents
    assert list(desktop.guard.resolve("vms", vm_id).glob("data_*/disk/overlay.qcow2")) == [original]
    assert set(desktop.get(vm_id).data_disks) == {"drive-data"}


async def test_cancelled_save_joins_allocation_and_releases_lock(service, monkeypatch):
    desktop, vm_id = await create_vm(service)
    entered, release = asyncio.Event(), asyncio.Event()
    run = managed_drives.run_tool

    async def delayed_create(*args, **kwargs):
        entered.set()
        await release.wait()
        return await run(*args, **kwargs)

    monkeypatch.setattr(managed_drives, "run_tool", delayed_create)
    task = asyncio.create_task(save_drives(desktop, vm_id, [blank()]))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    await task
    assert data_path(desktop, vm_id).is_file()
    await save_drives(desktop, vm_id, [])


async def test_published_definition_failure_retains_new_disk(service, monkeypatch):
    from palette import definitions

    desktop, vm_id = await create_vm(service)

    def published_then_failed(_):
        raise OSError("failure after publication")

    monkeypatch.setattr(definitions, "sync_directory", published_then_failed)
    with pytest.raises(OSError):
        await save_drives(desktop, vm_id, [blank()])
    assert not desktop.get(vm_id).data_disks
    restored = DesktopManager(service)
    path = data_path(restored, vm_id)
    assert path.exists() and path.parents[1] in desktop._extra_pins


async def test_running_save_does_not_allocate(service):
    desktop, vm_id = await create_vm(service)
    await desktop.start(vm_id)
    with pytest.raises(PaletteError) as error:
        await save_drives(desktop, vm_id, [blank()])
    assert error.value.code == ErrorCode.STATE_CONFLICT
    assert not list(desktop.guard.resolve("vms", vm_id).glob("data_*"))


async def delete_disk(desktop, vm_id, drive_id="drive-data", revision=None):
    return await delete_managed_disk(
        desktop,
        vm_id,
        {
            "revision": desktop.get(vm_id).revision if revision is None else revision,
            "drive_id": drive_id,
        },
    )


@pytest.mark.parametrize("retained", [False, True])
async def test_delete_owned_disk_releases_storage_and_preserves_other_media(service, retained):
    desktop, vm_id = await create_vm(service)
    await save_drives(desktop, vm_id, [blank(), blank("drive-keep", size=3)])
    path = data_path(desktop, vm_id)
    keep = (
        path.parents[2]
        / desktop.get(vm_id).data_disks["drive-keep"].owner
        / "disk"
        / "overlay.qcow2"
    )
    contents = keep.read_bytes()
    base = desktop.get(vm_id).profile.images.base
    if retained:
        definition = desktop.get(vm_id)
        settings = desktop.settings(definition).model_copy(
            update={"drives": desktop.settings(definition).drives[1:]}
        )
        desktop._save(
            definition.model_copy(
                update={"draft": definition.draft.model_copy(update={"settings": settings})}
            )
        )
    previous = desktop.get(vm_id).revision
    assert await delete_disk(desktop, vm_id) == {"deleted": "drive-data", "revision": previous + 1}
    assert not path.exists() and not path.parents[1].exists()
    assert keep.read_bytes() == contents
    assert "drive-data" not in DesktopManager(service).get(vm_id).data_disks
    assert all(drive.id != "drive-data" for drive in desktop.settings(desktop.get(vm_id)).drives)
    assert Path(base).exists()
    await desktop.start(vm_id)
    assert all(
        volume.device_id != "drive-data" for volume in service.manager.find(vm_id).overrides.volumes
    )


@pytest.mark.parametrize("damage", ["stale", "running", "external", "unsafe", "manifest"])
async def test_disk_delete_rejects_invalid_or_unsafe_requests(service, damage):
    desktop, vm_id = await create_vm(service)
    await save_drives(desktop, vm_id, [blank()])
    path = data_path(desktop, vm_id)
    revision = desktop.get(vm_id).revision
    drive_id = "drive-data"
    if damage == "stale":
        revision -= 1
    elif damage == "running":
        await desktop.start(vm_id)
    elif damage == "external":
        await save_drives(
            desktop,
            vm_id,
            [
                {
                    "id": "drive-import",
                    "media": "disk",
                    "path": service.config.profile("default").images.base,
                    "format": "qcow2",
                },
                blank(),
            ],
        )
        drive_id = "drive-import"
        revision = desktop.get(vm_id).revision
    elif damage == "unsafe":
        drive_id = "../../disk"
    else:
        manifest = path.parents[1] / "manifest.json"
        data = json.loads(manifest.read_text())
        data["data_drive_id"] = "drive-other"
        manifest.write_text(json.dumps(data), encoding="utf-8")
    before = desktop.get(vm_id).model_dump()
    with pytest.raises(PaletteError):
        await delete_disk(desktop, vm_id, drive_id, revision)
    assert path.exists() and desktop.get(vm_id).model_dump() == before


@pytest.mark.parametrize("reference", ["device", "other-vm", "base", "backing", "in-flight"])
async def test_disk_delete_protects_all_references(service, reference):
    from pathlib import Path

    from palette.vm.launcher import ExtraVolume, LaunchOverrides

    desktop, vm_id = await create_vm(service)
    await save_drives(desktop, vm_id, [blank()])
    path = data_path(desktop, vm_id)
    if reference == "in-flight":
        with (
            service.manager.storage.protect(
                desktop.get(vm_id).profile,
                LaunchOverrides(volumes=(ExtraVolume(path=str(path), media="disk"),)),
                None,
            ),
            pytest.raises(PaletteError),
        ):
            await delete_disk(desktop, vm_id)
        assert path.exists()
        return
    other_id = vm_id
    if reference != "device":
        other = await desktop.create(
            {**desktop.get(vm_id).draft.model_dump(), "name": "Reference", "settings": None}
        )
        other_id = other["vm_id"]
    if reference == "base":
        other = desktop.get(other_id)
        desktop._save(
            other.model_copy(
                update={
                    "profile": other.profile.model_copy(
                        update={
                            "images": other.profile.images.model_copy(update={"base": str(path)})
                        }
                    )
                }
            )
        )
    else:
        imported = path
        if reference == "backing":
            imported = Path(service.config.runtime_root) / "dependent.qcow2"
            metadata = {
                "driver": "qcow2",
                "backing": str(path),
                "backing_format": "qcow2",
                "virtual_size": 2 * 1024**3,
            }
            imported.write_bytes(b"QFI\xfb" + json.dumps(metadata).encode())
        drives = [{**blank(), "read_only": True}] if other_id == vm_id else []
        drives.append(
            {"id": "drive-ref", "media": "disk", "path": str(imported), "format": "qcow2"}
        )
        await save_drives(desktop, other_id, drives)
    with pytest.raises(PaletteError, match="引用"):
        await delete_disk(desktop, vm_id)
    assert path.exists() and not desktop.get(vm_id).data_disks["drive-data"].deleting


@pytest.mark.parametrize("failure", ["initial-save", "unlink", "final-save", "published"])
async def test_disk_delete_failures_remain_recoverable(service, monkeypatch, failure):
    from pathlib import Path

    from palette import definitions

    desktop, vm_id = await create_vm(service)
    await save_drives(desktop, vm_id, [blank()])
    path = data_path(desktop, vm_id)
    save = desktop._save
    calls = 0

    def fail_save(definition):
        nonlocal calls
        calls += 1
        if (failure == "initial-save" and calls == 1) or (failure == "final-save" and calls == 2):
            raise OSError("save failed")
        save(definition)

    def fail_unlink(target, *args, **kwargs):
        if target == path:
            raise PermissionError("in use")
        return unlink(target, *args, **kwargs)

    def fail_sync(_):
        raise OSError("publication already completed")

    with monkeypatch.context() as patch:
        patch.setattr(desktop, "_save", fail_save)
        if failure == "unlink":
            unlink = Path.unlink
            patch.setattr(Path, "unlink", fail_unlink)
        if failure == "published":
            patch.setattr(definitions, "sync_directory", fail_sync)
        with pytest.raises(PaletteError):
            await delete_disk(desktop, vm_id)
    record = desktop.get(vm_id).data_disks["drive-data"]
    assert record.deleting == (failure != "initial-save")
    assert path.exists() == (failure != "final-save")
    # Reloading must preserve a pending deletion without recreating a missing disk.
    restored = DesktopManager(service)
    assert restored.get(vm_id).data_disks["drive-data"].deleting == record.deleting
    await delete_disk(restored, vm_id)
    assert not path.exists() and "drive-data" not in restored.get(vm_id).data_disks


@pytest.mark.parametrize("cancel", [False, True])
async def test_inflight_checks_reject_new_references_and_allow_cancellation(
    service, monkeypatch, cancel
):
    from palette.vm.images import ImageManager
    from palette.vm.launcher import ExtraVolume, LaunchOverrides

    desktop, vm_id = await create_vm(service)
    await save_drives(desktop, vm_id, [blank()])
    path = data_path(desktop, vm_id)
    entered, release = asyncio.Event(), asyncio.Event()
    inspect = ImageManager.inspect_image

    async def delayed_inspect(images, *args, **kwargs):
        entered.set()
        await release.wait()
        return await inspect(images, *args, **kwargs)

    monkeypatch.setattr(ImageManager, "inspect_image", delayed_inspect)
    task = asyncio.create_task(delete_disk(desktop, vm_id))
    await entered.wait()
    if cancel:
        task.cancel()
        release.set()
        await task
        assert not path.exists() and "drive-data" not in desktop.get(vm_id).data_disks
        return
    else:
        with service.manager.storage.protect(
            desktop.get(vm_id).profile,
            LaunchOverrides(volumes=(ExtraVolume(path=str(path), media="disk"),)),
            None,
        ):
            release.set()
            with pytest.raises(PaletteError, match="引用发生变化"):
                await task
    assert path.exists() and not desktop.get(vm_id).data_disks["drive-data"].deleting


async def test_disk_delete_is_available_over_operator_ipc(service):
    from palette.operator import OperatorClient
    from palette.runtime import RuntimeOwner

    desktop, vm_id = await create_vm(service)
    await save_drives(desktop, vm_id, [blank()])
    path = data_path(desktop, vm_id)
    owner = RuntimeOwner(service, kind="embedded")
    await owner.start()
    try:
        client = OperatorClient(service.config.runtime_root)
        response = await client.call(
            "vms.disk.delete",
            vm_id=vm_id,
            payload={
                "revision": desktop.get(vm_id).revision,
                "drive_id": "drive-data",
            },
        )
        assert response["deleted"] == "drive-data"
        assert not path.exists()
    finally:
        await owner.close()
