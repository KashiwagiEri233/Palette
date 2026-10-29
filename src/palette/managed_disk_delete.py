"""Explicit deletion of owned data disks; external images are never deletion targets."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from .definitions import DiskDelete, ManagedDisk, VmDefinition
from .errors import ErrorCode, PaletteError
from .managed_drives import managed_disk_path
from .primary_drive import PRIMARY_DRIVE_ID, primary_present

if TYPE_CHECKING:
    from .config import Profile
    from .desktop import DesktopManager


def _owned_paths(
    desktop: DesktopManager, definition: VmDefinition, drive_id: str, record: ManagedDisk
) -> tuple[Path, Path]:
    root = desktop.guard.resolve("vms", definition.vm_id, record.owner)
    path = managed_disk_path(desktop, definition.vm_id, record)
    if root.exists():
        manifest = desktop.service.manager.images_for(definition.profile).read_manifest(
            definition.vm_id, record.owner
        )
        if (
            not manifest
            or not (
                manifest.get("data_drive_id") == drive_id
                or (
                    drive_id == PRIMARY_DRIVE_ID
                    and (
                        record.owner == definition.storage_owner
                        or manifest.get("storage_owner") == record.owner
                    )
                )
            )
            or manifest.get("process_exited") is not True
        ):
            raise PaletteError(ErrorCode.PATH_DENIED, "数据磁盘归属或使用状态无法确认, 不能删除")
    return root, path


def _snapshot(desktop: DesktopManager) -> tuple[Any, ...]:
    manager = desktop.service.manager
    return (
        tuple(desktop.definitions.items()),
        tuple(
            (key, instance.view.instance_id)
            for key in manager.known()
            if (instance := manager.find(key)) is not None
        ),
        manager.storage.referenced_paths(),
    )


async def _require_unreferenced(
    desktop: DesktopManager, definition: VmDefinition, drive_id: str, root: Path
) -> None:
    """Inspect persisted and live references, then commit without another await.

    The snapshot rejects concurrent catalog/start changes during asynchronous image
    inspection. Storage protects asynchronous starts before their QEMU process exists.
    """
    manager = desktop.service.manager
    snapshot = _snapshot(desktop)
    candidates: dict[Path, Profile] = {}

    def add(path: str | Path | None, profile: Profile) -> None:
        if path:
            candidates[Path(path).resolve()] = profile

    for profile in desktop.service.config.profiles.values():
        add(profile.images.base, profile)
    for original in desktop.definitions.values():
        item = definition if original.vm_id == definition.vm_id else original
        add(item.profile.images.base, item.profile)
        if (
            item.storage_owner
            and not (item.vm_id == definition.vm_id and drive_id == PRIMARY_DRIVE_ID)
            and primary_present(item)
        ):
            add(
                desktop.guard.resolve(
                    "vms", item.vm_id, item.storage_owner, "disk", "overlay.qcow2"
                ),
                item.profile,
            )
        for key, disk in item.data_disks.items():
            if item.vm_id != definition.vm_id or key != drive_id:
                add(
                    managed_disk_path(desktop, item.vm_id, disk),
                    item.profile,
                )
        for drive in desktop.settings(item).drives:
            if item.vm_id == definition.vm_id and drive.id == drive_id:
                continue
            add(drive.path, item.profile)
    for key in manager.known():
        instance = manager.find(key)
        if instance is None or (
            instance.state in {"STOPPED", "FAILED"}
            and (instance.process is None or instance.process.returncode is not None)
        ):
            continue
        add(instance.profile.images.base, instance.profile)
        add(instance.paths.overlay, instance.profile)
        add(instance.overrides.image, instance.profile)
        for volume in instance.overrides.volumes:
            add(volume.path, instance.profile)
    for path in manager.storage.referenced_paths():
        add(path, definition.profile)

    inspected: dict[Path, tuple[int, int]] = {}
    for path, profile in candidates.items():
        if path.is_relative_to(root):
            raise PaletteError(ErrorCode.STATE_CONFLICT, "磁盘仍被其他设备、虚拟机或配置引用")
        if not path.is_file():
            continue
        stat = path.stat()
        inspected[path] = (stat.st_mtime_ns, stat.st_size)
        info = await manager.images_for(profile).inspect_image(path)
        if any(entry.is_relative_to(root) for entry in info.chain):
            raise PaletteError(ErrorCode.STATE_CONFLICT, "磁盘仍被其他镜像的 backing chain 引用")
    if snapshot != _snapshot(desktop) or any(
        not path.is_file() or (path.stat().st_mtime_ns, path.stat().st_size) != stamp
        for path, stamp in inspected.items()
    ):
        raise PaletteError(ErrorCode.STATE_CONFLICT, "磁盘引用发生变化, 请重试删除")


def _save_state(desktop: DesktopManager, definition: VmDefinition) -> None:
    try:
        desktop._save(definition)
    except OSError as exc:
        # A directory fsync can fail after replace published the new definition.
        # Reflect that exact publication in memory so a retry cannot use stale state.
        target = desktop.guard.resolve("definitions", definition.vm_id + ".json")
        try:
            saved = VmDefinition.model_validate_json(target.read_text(encoding="utf-8"))
            if saved == definition:
                desktop.definitions[definition.vm_id] = saved
                desktop._pin()
        except (OSError, ValueError):
            pass
        raise PaletteError(ErrorCode.STATE_CONFLICT, "磁盘删除状态保存失败, 请刷新后重试") from exc


async def delete_managed_disk(
    desktop: DesktopManager, vm_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Explicit operator shortcut through the same settings-save transaction."""
    async with desktop.service.manager.operations.hold(vm_id):
        definition = desktop.get(vm_id)
        desktop.storage.require_stopped(vm_id)
        try:
            request = DiskDelete.model_validate(payload)
        except ValidationError as exc:
            raise desktop._validation_error(exc) from exc
        desktop._revision(definition, request.revision)
        if request.drive_id not in definition.data_disks and not (
            request.drive_id == PRIMARY_DRIVE_ID and primary_present(definition)
        ):
            raise PaletteError(ErrorCode.INVALID_ARGUMENT, "只能删除 Palette 创建的数据磁盘")
        settings = desktop.settings(definition)
        result = await desktop.update(
            vm_id,
            {
                "revision": request.revision,
                "name": definition.draft.name,
                "vcpus": definition.draft.vcpus,
                "memory_mib": definition.draft.memory_mib,
                "network": definition.draft.network,
                "iso_attached": False,
                "settings": settings.model_copy(
                    update={
                        "drives": [
                            drive for drive in settings.drives if drive.id != request.drive_id
                        ]
                    }
                ).model_dump(),
                "delete_drives": [request.drive_id],
            },
        )
        if request.drive_id in result["pending_disk_deletions"]:
            raise PaletteError(ErrorCode.STATE_CONFLICT, "配置已保存, 磁盘文件清理失败, 可重试")
        return {"deleted": request.drive_id, "revision": result["revision"]}
