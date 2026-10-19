"""Persistent disk transactions, expansion journals and recovery."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from .config import Profile
from .definitions import (
    DefinitionRepository,
    DiskResize,
    ResizeOperation,
    VmDefinition,
    sync_directory,
)
from .errors import ErrorCode, PaletteError
from .policy import validate_identifier
from .vm.images import InstancePaths
from .vm.launcher import run_tool
from .vm.managed import finish_cleanup
from .vm.manager import VmState

if TYPE_CHECKING:
    from .service import PaletteService


class PersistentStorage:
    def __init__(self, service: PaletteService, repository: DefinitionRepository) -> None:
        self.service = service
        self.repository = repository
        self.guard = repository.guard
        self.definitions = repository.definitions

    def get(self, vm_id: str) -> VmDefinition:
        return self.repository.get(vm_id)

    def _save(self, definition: VmDefinition) -> None:
        self.repository.save(definition)

    @staticmethod
    def _validation_error(exc: ValidationError) -> PaletteError:
        fields = [
            ".".join(str(part) for part in error["loc"]) + ": " + error["msg"]
            for error in exc.errors(include_input=False)
        ]
        return PaletteError(ErrorCode.INVALID_ARGUMENT, "; ".join(fields))

    def _revision(self, definition: VmDefinition, revision: int) -> None:
        if revision != definition.revision:
            raise PaletteError(ErrorCode.STATE_CONFLICT, "配置已被修改; 请重新打开设置后重试")

    def operation_path(self, vm_id: str) -> Path:
        validate_identifier(vm_id, what="vm_id")
        return self.guard.resolve("definitions", "operations", vm_id + ".json")

    @staticmethod
    def _write_operation(path: Path, record: dict[str, Any]) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)

    async def recover_resize(self, vm_id: str) -> None:
        path = self.operation_path(vm_id)
        if not path.exists():
            return
        self.require_stopped(vm_id)
        self.guard.assert_managed(path)
        if path.stat().st_size > 256 * 1024:
            raise PaletteError(ErrorCode.IMAGE_INVALID, "disk expansion journal exceeds its limit")
        try:
            record = ResizeOperation.model_validate_json(
                path.read_text(encoding="utf-8")
            ).model_dump()
        except (ValidationError, ValueError) as exc:
            raise PaletteError(
                ErrorCode.IMAGE_INVALID, "invalid disk expansion journal; startup blocked"
            ) from exc
        definition = self.get(vm_id)
        storage = self.paths(definition)
        if storage is None or record["overlay"] != str(storage.overlay):
            raise PaletteError(ErrorCode.IMAGE_INVALID, "扩容记录与受管理磁盘不一致")
        images = self.service.manager.images_for(definition.profile)
        info = await images.inspect_image(storage.overlay, image_format="qcow2")
        if [str(entry) for entry in info.chain] != record["chain"] or info.virtual_size not in {
            record["before_bytes"],
            record["target_bytes"],
        }:
            raise PaletteError(ErrorCode.IMAGE_INVALID, "扩容后磁盘容量或 backing chain 无法确认")
        code, _, _ = await run_tool(
            [*images.qemu_img_argv, "check", "-q", "-f", "qcow2", str(storage.overlay)], timeout=120
        )
        if code:
            raise PaletteError(ErrorCode.IMAGE_INVALID, "磁盘完整性检查失败; 已阻止启动")
        if info.virtual_size == record["target_bytes"]:
            if definition.draft.disk_gib != record["disk_gib"]:
                self._commit_capacity(definition, record["disk_gib"])
            images.update_manifest(storage, {"virtual_size": info.virtual_size})
        path.unlink()
        sync_directory(path.parent)

    def _commit_capacity(self, definition: VmDefinition, disk_gib: int) -> None:
        draft = definition.draft.model_copy(update={"disk_gib": disk_gib})
        profile = definition.profile.model_copy(
            update={"images": definition.profile.images.model_copy(update={"size_gib": disk_gib})}
        )
        self._save(
            definition.model_copy(
                update={
                    "schema_version": 2,
                    "revision": definition.revision + 1,
                    "draft": draft,
                    "profile": profile,
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            )
        )

    async def resize_disk(self, vm_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with self.service.manager.operations.hold(vm_id):
            await self.recover_resize(vm_id)
            self.require_stopped(vm_id)
            definition = self.get(vm_id)
            try:
                request = DiskResize.model_validate(payload)
            except ValidationError as exc:
                raise self._validation_error(exc) from exc
            self._revision(definition, request.revision)
            if request.disk_gib <= definition.draft.disk_gib:
                raise PaletteError(ErrorCode.INVALID_ARGUMENT, "磁盘只能扩容; 不能缩小或保持原容量")
            images = self.service.manager.images_for(definition.profile)
            storage = self.paths(definition)
            if storage is None:
                base = (
                    await images.inspect_image(Path(definition.profile.images.base))
                    if definition.draft.source == "image"
                    else None
                )
                if base and request.disk_gib * 1024**3 < base.virtual_size:
                    raise PaletteError(ErrorCode.INVALID_ARGUMENT, "容量不能小于基础镜像")
                self._commit_capacity(definition, request.disk_gib)
            else:
                info = await images.inspect_image(storage.overlay, image_format="qcow2")
                if request.disk_gib * 1024**3 <= info.virtual_size:
                    raise PaletteError(ErrorCode.INVALID_ARGUMENT, "目标容量必须大于实盘容量")
                code, _, _ = await run_tool(
                    [*images.qemu_img_argv, "check", "-q", "-f", "qcow2", str(storage.overlay)],
                    timeout=120,
                )
                if code:
                    raise PaletteError(ErrorCode.IMAGE_INVALID, "原磁盘完整性检查失败; 未执行扩容")
                self._write_operation(
                    self.operation_path(vm_id),
                    {
                        "overlay": str(storage.overlay),
                        "chain": [str(entry) for entry in info.chain],
                        "before_bytes": info.virtual_size,
                        "target_bytes": request.disk_gib * 1024**3,
                        "disk_gib": request.disk_gib,
                    },
                )

                async def resize() -> None:
                    code, _, _ = await run_tool(
                        [
                            *images.qemu_img_argv,
                            "resize",
                            "-f",
                            "qcow2",
                            str(storage.overlay),
                            f"{request.disk_gib}G",
                        ],
                        timeout=120,
                    )
                    await self.recover_resize(vm_id)
                    if code or self.get(vm_id).draft.disk_gib != request.disk_gib:
                        raise PaletteError(
                            ErrorCode.IMAGE_INVALID, "磁盘扩容失败; 请检查当前实盘容量后重试"
                        )

                await finish_cleanup(asyncio.create_task(resize()))
            return {
                "revision": self.get(vm_id).revision,
                "disk_gib": self.get(vm_id).draft.disk_gib,
            }

    def require_stopped(self, vm_id: str) -> None:
        instance = self.service.manager.find(vm_id)
        if instance is not None and (
            instance.state not in {VmState.STOPPED, VmState.FAILED}
            or (instance.process is not None and instance.process.returncode is None)
        ):
            raise PaletteError(
                ErrorCode.STATE_CONFLICT, "shut down the VM before editing or deleting it"
            )
        definition = self.definitions.get(vm_id)
        if definition and definition.storage_owner:
            self.paths(definition)

    def paths(self, definition: VmDefinition) -> InstancePaths | None:
        owner = definition.storage_owner
        if owner is None:
            return None
        images = self.service.manager.images_for(definition.profile)
        record = images.read_manifest(definition.vm_id, owner)
        if record is None or record.get("process_exited") is not True:
            raise PaletteError(
                ErrorCode.IMAGE_INVALID, "storage is missing or its previous process has not exited"
            )
        root = self.guard.resolve("vms", definition.vm_id, owner)
        overlay = self.guard.resolve("vms", definition.vm_id, owner, "disk", "overlay.qcow2")
        variables = self.guard.resolve("vms", definition.vm_id, owner, "disk", "vars.fd")
        if not overlay.is_file() or (record.get("hardware_vars") and not variables.is_file()):
            raise PaletteError(
                ErrorCode.IMAGE_MISSING, "persistent disk or UEFI variables are missing"
            )
        return InstancePaths(
            definition.vm_id,
            owner,
            root,
            root / "disk",
            overlay,
            root / "frames",
            root / "logs",
            root / "manifest.json",
            variables if variables.is_file() else None,
        )

    async def seed(self, definition: VmDefinition) -> VmDefinition:
        if definition.draft.source != "iso" or definition.seed_owner is not None:
            return definition
        images = self.service.manager.images_for(definition.profile)
        paths = images.allocate_instance(definition.vm_id, "seed_" + uuid.uuid4().hex[:12])
        base = paths.disk_dir / "base.qcow2"
        if not images.qemu_img_argv:
            raise PaletteError(ErrorCode.QEMU_NOT_FOUND, "qemu-img is required to create a disk")
        code, _, _ = await run_tool(
            [
                *images.qemu_img_argv,
                "create",
                "-f",
                "qcow2",
                str(base),
                f"{definition.draft.disk_gib}G",
            ],
            timeout=60,
        )
        if code:
            raise PaletteError(ErrorCode.IMAGE_INVALID, "qemu-img could not create the blank disk")
        images.update_manifest(
            paths, {"base_image": str(base), "state": "STOPPED", "process_exited": True}
        )
        data = definition.profile.model_dump()
        data["images"].update(base=str(base), format="qcow2", sha256=None)
        updated = definition.model_copy(
            update={"profile": Profile.model_validate(data), "seed_owner": paths.instance_id}
        )
        self._save(updated)
        return updated
