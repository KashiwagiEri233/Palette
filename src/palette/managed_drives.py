"""Transactional creation and retention of blank, persistent data disks."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from .definitions import ManagedDisk, VmDefinition
from .errors import ErrorCode, PaletteError
from .primary_drive import PRIMARY_DRIVE_ID, primary_present
from .vm.launcher import run_tool
from .vm.managed import finish_cleanup
from .vm_settings import DriveSettings

if TYPE_CHECKING:
    from .desktop import DesktopManager

logger = logging.getLogger("palette.managed_drives")


def managed_disk_path(desktop: DesktopManager, vm_id: str, record: ManagedDisk) -> Path:
    return desktop.guard.resolve(
        "vms",
        vm_id,
        record.owner,
        "disk",
        "disk.raw" if record.format == "raw" else "overlay.qcow2",
    )


def managed_drive_path(
    desktop: DesktopManager, definition: VmDefinition, drive: DriveSettings
) -> Path:
    record = definition.data_disks.get(drive.id)
    if record is None:
        raise PaletteError(ErrorCode.IMAGE_MISSING, "受管理数据磁盘尚未创建")
    if record.deleting:
        raise PaletteError(ErrorCode.STATE_CONFLICT, "数据磁盘正在删除, 请先完成删除")
    if record.size_gib != drive.size_gib:
        raise PaletteError(ErrorCode.STATE_CONFLICT, "已创建的数据磁盘容量不能直接修改")
    if drive.format != record.format:
        raise PaletteError(ErrorCode.STATE_CONFLICT, "已创建磁盘的镜像格式不可直接修改")
    path = managed_disk_path(desktop, definition.vm_id, record)
    manifest = desktop.service.manager.images_for(definition.profile).read_manifest(
        definition.vm_id, record.owner
    )
    if not path.is_file() or not manifest or manifest.get("data_drive_id") != drive.id:
        raise PaletteError(ErrorCode.IMAGE_MISSING, "受管理数据磁盘缺失, 已阻止重新创建以保护数据")
    return path


async def save_with_managed_drives(
    desktop: DesktopManager, definition: VmDefinition, *, delete_ids: set[str] | None = None
) -> VmDefinition:
    """Caller holds the VM operation lock; cancellation joins the whole transaction."""
    return await finish_cleanup(
        asyncio.create_task(_save(desktop, definition, delete_ids or set()))
    )


async def _save(
    desktop: DesktopManager, definition: VmDefinition, delete_ids: set[str]
) -> VmDefinition:
    from .managed_disk_delete import _owned_paths, _require_unreferenced, _save_state, _snapshot

    images = desktop.service.manager.images_for(definition.profile)
    records = dict(definition.data_disks)
    if PRIMARY_DRIVE_ID in delete_ids and primary_present(definition):
        raise PaletteError(ErrorCode.INVALID_ARGUMENT, "删除目标必须为已移除的受管磁盘")
    if PRIMARY_DRIVE_ID in delete_ids and PRIMARY_DRIVE_ID not in records:
        if definition.storage_owner is None:
            delete_ids = delete_ids - {PRIMARY_DRIVE_ID}
        else:
            records[PRIMARY_DRIVE_ID] = ManagedDisk(
                owner=definition.storage_owner,
                size_gib=definition.draft.disk_gib,
            )
    created = []
    drives = []
    try:
        for drive in desktop.settings(definition).drives:
            if drive.id == PRIMARY_DRIVE_ID:
                if (
                    not primary_present(definition)
                    or drive.media != "disk"
                    or drive.path is not None
                    or drive.format != "qcow2"
                    or drive.import_copy
                    or drive.size_gib != definition.draft.disk_gib
                    or drive.id in records
                ):
                    raise PaletteError(
                        ErrorCode.INVALID_ARGUMENT, "已创建磁盘的容量或格式不可直接修改"
                    )
                drives.append(drive)
                continue
            if drive.size_gib is None and not drive.import_copy:
                if drive.id in records:
                    raise PaletteError(
                        ErrorCode.STATE_CONFLICT, "不能将受管理数据磁盘替换为外部镜像"
                    )
                drives.append(drive)
                continue
            if drive.id in records:
                path = managed_drive_path(desktop, definition, drive)
                info = await images.inspect_image(path)
            else:
                if len(records) >= 256:
                    raise PaletteError(ErrorCode.STATE_CONFLICT, "保留的数据磁盘数量已达上限")
                if not images.qemu_img_argv:
                    raise PaletteError(ErrorCode.QEMU_NOT_FOUND, "创建空白磁盘需要 qemu-img")
                paths = images.allocate_instance(definition.vm_id, "data_" + uuid.uuid4().hex[:12])
                created.append(paths)
                desktop._extra_pins.add(paths.root)
                desktop._pin()
                path = paths.disk_dir / ("disk.raw" if drive.format == "raw" else "overlay.qcow2")
                source = None
                if drive.import_copy:
                    from .vm.images import validate_caller_path

                    source = validate_caller_path(drive.path or "", what="导入磁盘")
                    source_info = await images.inspect_image(source)
                    if not 0 < source_info.virtual_size <= 4096 * 1024**3:
                        raise PaletteError(
                            ErrorCode.INVALID_ARGUMENT, "导入磁盘容量必须大于零且不超过 4096 GiB"
                        )
                    command = [
                        *images.qemu_img_argv,
                        "convert",
                        "-f",
                        source_info.format,
                        "-O",
                        drive.format,
                        str(source),
                        str(path),
                    ]
                else:
                    command = [
                        *images.qemu_img_argv,
                        "create",
                        "-f",
                        drive.format,
                        str(path),
                        f"{drive.size_gib}G",
                    ]
                code, _, _ = await run_tool(command, timeout=300 if source else 60)
                if code:
                    raise PaletteError(ErrorCode.IMAGE_INVALID, "创建或导入数据磁盘失败")
                info = await images.inspect_image(path)
                size_gib = math.ceil(info.virtual_size / 1024**3)
                if source and info.virtual_size != source_info.virtual_size:
                    raise PaletteError(ErrorCode.IMAGE_INVALID, "导入磁盘容量与源镜像不一致")
                images.update_manifest(
                    paths,
                    {
                        "state": "STOPPED",
                        "process_exited": True,
                        "data_drive_id": drive.id,
                        "virtual_size": info.virtual_size,
                        "overlay": str(path),
                    },
                )
                records[drive.id] = ManagedDisk(
                    owner=paths.instance_id,
                    size_gib=size_gib,
                    virtual_size=info.virtual_size,
                    format=drive.format,
                    source_name=source.name if source else None,
                )
            record = records[drive.id]
            expected = managed_disk_path(desktop, definition.vm_id, record)
            if (
                info.format != record.format
                or info.chain != (expected,)
                or info.virtual_size != (record.virtual_size or record.size_gib * 1024**3)
            ):
                raise PaletteError(ErrorCode.IMAGE_INVALID, "受管理数据磁盘格式、容量或镜像链异常")
            drives.append(
                drive.model_copy(
                    update={
                        "path": None,
                        "size_gib": record.size_gib,
                        "import_copy": False,
                        "format": record.format,
                    }
                )
            )
        updated = definition.model_copy(
            update={
                "data_disks": records,
                "draft": definition.draft.model_copy(
                    update={
                        "settings": desktop.settings(definition).model_copy(
                            update={"drives": drives}
                        )
                        if definition.draft.settings is not None or drives
                        else None
                    }
                ),
            }
        )
        if delete_ids & {drive.id for drive in drives} or not delete_ids <= records.keys():
            raise PaletteError(ErrorCode.INVALID_ARGUMENT, "删除目标必须为已移除的受管磁盘")
        snapshot = _snapshot(desktop)
        for drive_id in delete_ids:
            root, _ = _owned_paths(desktop, updated, drive_id, records[drive_id])
            await _require_unreferenced(desktop, updated, drive_id, root)
            records[drive_id] = records[drive_id].model_copy(update={"deleting": True})
        if delete_ids and snapshot != _snapshot(desktop):
            raise PaletteError(ErrorCode.STATE_CONFLICT, "磁盘引用发生变化, 请重试保存")
        for drive_id in delete_ids:
            _owned_paths(desktop, updated, drive_id, records[drive_id])
        updated = updated.model_copy(update={"data_disks": dict(records)})
        if delete_ids:
            _save_state(desktop, updated)
        else:
            desktop._save(updated)
        for drive_id in delete_ids:
            record = records[drive_id]
            try:
                root, path = _owned_paths(desktop, updated, drive_id, record)
                path.unlink(missing_ok=True)
            except (OSError, PaletteError):
                logger.warning("saved disk deletion needs retry", exc_info=True)
                continue
            records.pop(drive_id)
            updated = updated.model_copy(update={"data_disks": dict(records)})
            _save_state(desktop, updated)
            if root.exists() and record.owner != updated.storage_owner:
                try:
                    images.delete_instance(definition.vm_id, record.owner)
                except (OSError, PaletteError):
                    logger.warning("deleted disk directory cleanup deferred", exc_info=True)
        return updated
    except BaseException:
        # Only erase newly allocated, uncommitted files. A replace/fsync failure may
        # have published the definition already; retain those disks conservatively.
        target = desktop.guard.resolve("definitions", definition.vm_id + ".json")
        referenced: set[str] | None = set()
        if target.exists():
            try:
                document = json.loads(target.read_text(encoding="utf-8"))
                referenced = {item["owner"] for item in document.get("data_disks", {}).values()}
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                referenced = None
        for paths in created:
            if referenced is None or paths.instance_id in referenced:
                continue
            try:
                images.delete_instance(definition.vm_id, paths.instance_id)
            except (OSError, PaletteError):
                logger.warning("uncommitted data disk cleanup failed", exc_info=True)
        raise
    finally:
        for paths in created:
            if paths.root.exists():
                # Keep uncertain disk publications pinned until the next reload.
                if any(
                    disk.owner == paths.instance_id
                    for disk in desktop.definitions.get(
                        definition.vm_id, definition
                    ).data_disks.values()
                ):
                    desktop._extra_pins.discard(paths.root)
            else:
                desktop._extra_pins.discard(paths.root)
        desktop._pin()
