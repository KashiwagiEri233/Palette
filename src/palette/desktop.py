"""Persistent operator-owned VM definitions; runtime instances remain disposable."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .config import Profile
from .errors import ErrorCode, PaletteError
from .policy import PathGuard, validate_identifier
from .vm.images import ImageManager, InstancePaths, validate_caller_path
from .vm.launcher import ExtraVolume, LaunchOverrides, run_tool
from .vm.managed import finish_cleanup
from .vm.manager import VmInstance, VmState
from .vm_settings import (
    CapabilityProbe,
    HardwareSettings,
    apply_settings,
    settings_from_profile,
    validate_hardware,
)

if TYPE_CHECKING:
    from .service import PaletteService


class VmDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    name: str = Field(min_length=1, max_length=120)
    profile_id: str
    source: Literal["image", "iso"]
    path: str = Field(min_length=1, max_length=4096)
    image_format: Literal["qcow2", "raw"] = "qcow2"
    vcpus: int = Field(ge=1, le=64)
    memory_mib: int = Field(ge=256, le=1048576)
    disk_gib: int = Field(default=24, ge=1, le=4096)
    network: Literal["offline", "restricted-lab", "nat"] = "restricted-lab"
    settings: HardwareSettings | None = None
    iso_attached: bool = True

    @field_validator("name")
    @classmethod
    def nonempty_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("VM name must not be blank")
        return value.strip()


class VmUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    revision: int = Field(ge=0)
    name: str = Field(min_length=1, max_length=120)
    vcpus: int = Field(ge=1, le=64)
    memory_mib: int = Field(ge=256, le=1048576)
    network: Literal["offline", "restricted-lab", "nat"]
    iso_attached: bool
    settings: HardwareSettings

    @field_validator("name")
    @classmethod
    def nonempty_name(cls, value: str) -> str:
        return VmDraft.nonempty_name(value)


class DiskResize(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: int = Field(ge=0)
    disk_gib: int = Field(ge=1, le=4096)


class ResizeOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    overlay: str
    chain: list[str] = Field(min_length=1, max_length=32)
    before_bytes: int = Field(ge=1)
    target_bytes: int = Field(ge=1)
    disk_gib: int = Field(ge=1, le=4096)

    @model_validator(mode="after")
    def consistent_capacity(self) -> ResizeOperation:
        if self.target_bytes != self.disk_gib * 1024**3 or self.target_bytes <= self.before_bytes:
            raise ValueError("invalid disk expansion journal")
        return self


def sync_directory(path: Path) -> None:
    if sys.platform != "win32":
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class VmDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1, 2] = 2
    revision: int = Field(default=0, ge=0)
    vm_id: str
    draft: VmDraft
    profile: Profile
    storage_owner: str | None = None
    seed_owner: str | None = None
    iso_attached: bool = True
    created_at: str
    updated_at: str


class DesktopManager:
    def __init__(self, service: PaletteService) -> None:
        self.service = service
        self.guard = PathGuard(service.config.runtime_root)
        self.directory = self.guard.resolve("definitions")
        self.definitions: dict[str, VmDefinition] = {}
        self._extra_pins: set[Path] = set()
        self._lock = asyncio.Lock()
        self.probe = CapabilityProbe(service.config.host)
        if self.directory.exists():
            for path in self.directory.glob("*.json"):
                self.guard.assert_managed(path)
                if path.stat().st_size > 256 * 1024:
                    raise PaletteError(
                        ErrorCode.INVALID_ARGUMENT, "VM definition exceeds its limit"
                    )
                definition = VmDefinition.model_validate_json(path.read_text(encoding="utf-8"))
                validate_identifier(definition.vm_id, what="vm_id")
                if path.stem != definition.vm_id:
                    raise PaletteError(
                        ErrorCode.INVALID_ARGUMENT, "VM definition ID does not match its file"
                    )
                self.definitions[definition.vm_id] = definition
        self._pin()

    def _pin(self) -> None:
        self.service.manager.persistent_roots = self._extra_pins | {
            self.guard.resolve("vms", definition.vm_id, owner)
            for definition in self.definitions.values()
            for owner in (definition.storage_owner, definition.seed_owner)
            if owner is not None
        }

    def _save(self, definition: VmDefinition) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = self.guard.resolve("definitions", definition.vm_id + ".json")
        temporary = self.guard.resolve("definitions", definition.vm_id + ".tmp")
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(definition.model_dump_json(indent=2))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        sync_directory(target.parent)
        self.definitions[definition.vm_id] = definition
        self._pin()

    def get(self, vm_id: str) -> VmDefinition:
        validate_identifier(vm_id, what="vm_id")
        try:
            return self.definitions[vm_id]
        except KeyError as exc:
            raise PaletteError(ErrorCode.VM_NOT_FOUND, "VM definition not found") from exc

    async def list(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for vm_id in sorted(set(self.definitions) | set(self.service.manager.known())):
            definition = self.definitions.get(vm_id)
            status = await self.service.status(vm_id, actor="operator")
            result.append(
                {
                    "vm_id": vm_id,
                    "name": definition.draft.name if definition else vm_id,
                    "persistent": definition is not None,
                    "definition": {
                        "draft": definition.draft.model_dump(mode="json"),
                        "iso_attached": definition.iso_attached,
                        "revision": definition.revision,
                    }
                    if definition
                    else None,
                    "status": status.status.model_dump(mode="json")
                    if status.status
                    else {
                        "state": "STOPPED",
                        "vm_id": vm_id,
                        "guest_arch": definition.profile.qemu.arch if definition else None,
                    },
                }
            )
        return result

    async def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            try:
                draft = VmDraft.model_validate(payload)
            except ValidationError as exc:
                raise self._validation_error(exc) from exc
            path = validate_caller_path(draft.path, what=draft.source)
            profile = self.service.config.profile(draft.profile_id)
            self._resource_conflicts(profile, draft.vcpus, draft.memory_mib)
            if draft.settings:
                await validate_hardware(self.probe, profile, draft.settings)
                self._check_conflicts(profile, draft.settings, None)
                profile = apply_settings(profile, draft.settings)
            data = profile.model_dump()
            data["resources"]["vcpus"] = draft.vcpus
            data["resources"]["memory_mib"] = draft.memory_mib
            if data["resources"]["process_memory_limit_mib"] is not None:
                data["resources"]["process_memory_limit_mib"] = max(
                    draft.memory_mib + 2048, data["resources"]["process_memory_limit_mib"]
                )
            data["network"]["mode"] = draft.network
            if draft.settings:
                data["network"]["hostfwd"] += [
                    self._forward(entry) for entry in draft.settings.network.forwards
                ]
            data["images"]["size_gib"] = draft.disk_gib
            if draft.source == "image":
                data["images"].update(base=str(path), format=draft.image_format, sha256=None)
            now = datetime.now(UTC).isoformat()
            definition = VmDefinition(
                vm_id="vm_" + uuid.uuid4().hex[:12],
                draft=draft.model_copy(update={"path": str(path)}),
                profile=Profile.model_validate(data),
                created_at=now,
                updated_at=now,
                iso_attached=draft.iso_attached,
            )
            self._validate_media(self.settings(definition), definition.iso_attached)
            self._save(definition)
            return definition.model_dump(mode="json")

    async def update(self, vm_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            await self._recover_resize(vm_id)
            definition = self.get(vm_id)
            self._require_stopped(vm_id)
            try:
                update = VmUpdate.model_validate(payload)
            except ValidationError as exc:
                raise self._validation_error(exc) from exc
            self._revision(definition, update.revision)
            self._resource_conflicts(definition.profile, update.vcpus, update.memory_mib)
            previous = self.settings(definition)
            await validate_hardware(self.probe, definition.profile, update.settings, previous)
            self._check_conflicts(definition.profile, update.settings, previous)
            self._validate_media(update.settings, update.iso_attached)
            data = definition.draft.model_dump()
            data.update(update.model_dump(exclude={"revision", "iso_attached"}))
            data["iso_attached"] = update.iso_attached
            draft = VmDraft.model_validate(data)
            profile_data = apply_settings(definition.profile, update.settings).model_dump()
            profile_data["resources"].update(vcpus=draft.vcpus, memory_mib=draft.memory_mib)
            limit = profile_data["resources"]["process_memory_limit_mib"]
            if limit is not None:
                profile_data["resources"]["process_memory_limit_mib"] = max(
                    limit, draft.memory_mib + 2048
                )
            profile_data["network"]["mode"] = draft.network
            old_forwards = [self._forward(entry) for entry in previous.network.forwards]
            preserved = [
                entry for entry in definition.profile.network.hostfwd if entry not in old_forwards
            ]
            profile_data["network"]["hostfwd"] = preserved + [
                self._forward(entry) for entry in update.settings.network.forwards
            ]
            profile_data["images"]["size_gib"] = draft.disk_gib
            profile = Profile.model_validate(profile_data)
            definition = definition.model_copy(
                update={
                    "schema_version": 2,
                    "revision": definition.revision + 1,
                    "draft": draft,
                    "profile": profile,
                    "iso_attached": payload.get("iso_attached", definition.iso_attached),
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            )
            self._save(definition)
            return definition.model_dump(mode="json")

    def settings(self, definition: VmDefinition) -> HardwareSettings:
        return definition.draft.settings or settings_from_profile(
            definition.profile, definition.draft.path if definition.draft.source == "iso" else None
        )

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

    def _running(self, vm_id: str) -> bool:
        instance = self.service.manager.find(vm_id)
        return instance is not None and (
            instance.state not in {VmState.STOPPED, VmState.FAILED}
            or (instance.process is not None and instance.process.returncode is None)
        )

    async def get_settings(self, vm_id: str) -> dict[str, Any]:
        async with self._lock:
            await self._recover_resize(vm_id)
            definition = self.get(vm_id)
            running = self._running(vm_id)
            disk: dict[str, Any] = {
                "configured_gib": definition.draft.disk_gib,
                "virtual_bytes": None,
                "actual_bytes": None,
                "allocated": definition.storage_owner is not None,
            }
            if definition.storage_owner:
                images = self.service.manager._images(definition.profile)
                if running:
                    storage = self._storage_after_stop(definition)
                    record = images.read_manifest(vm_id, definition.storage_owner) or {}
                    disk.update(
                        virtual_bytes=record.get(
                            "virtual_size", definition.draft.disk_gib * 1024**3
                        )
                    )
                    stat = storage.overlay.stat()
                    disk["actual_bytes"] = getattr(stat, "st_blocks", stat.st_size // 512) * 512
                else:
                    stopped_storage = self._storage(definition)
                    if stopped_storage:
                        info = await images.inspect_image(
                            stopped_storage.overlay, image_format="qcow2"
                        )
                        disk.update(virtual_bytes=info.virtual_size, actual_bytes=info.actual_size)
            return {
                "revision": definition.revision,
                "draft": definition.draft.model_dump(mode="json"),
                "settings": self.settings(definition).model_dump(mode="json"),
                "iso_attached": definition.iso_attached,
                "editable": not running,
                "capabilities": await self.probe.get(definition.profile),
                "disk": disk,
                "preserved_network": {
                    "extra_options": definition.profile.network.extra_options,
                    "hostfwd": [
                        entry
                        for entry in definition.profile.network.hostfwd
                        if entry
                        not in [
                            self._forward(rule)
                            for rule in self.settings(definition).network.forwards
                        ]
                    ],
                },
            }

    @staticmethod
    def _forward(entry: Any) -> str:
        return f"{entry.protocol}:127.0.0.1:{entry.host_port}-:{entry.guest_port}"

    @staticmethod
    def _validate_media(settings: HardwareSettings, attached: bool) -> None:
        if settings.iso_path and attached:
            validate_caller_path(settings.iso_path, what="ISO")
        if settings.boot_order == "cdrom" and not (settings.iso_path and attached):
            raise PaletteError(ErrorCode.INVALID_ARGUMENT, "光盘优先需要挂载 ISO")

    @staticmethod
    def _resource_conflicts(profile: Profile, vcpus: int, memory_mib: int) -> None:
        flags = {argument.split("=", 1)[0] for argument in profile.qemu.extra_args}
        if (vcpus != profile.resources.vcpus and "-smp" in flags) or (
            memory_mib != profile.resources.memory_mib and "-m" in flags
        ):
            raise PaletteError(
                ErrorCode.STATE_CONFLICT, "profile 的 CPU 或内存附加参数与资源修改冲突"
            )

    @staticmethod
    def _check_conflicts(
        profile: Profile, settings: HardwareSettings, previous: HardwareSettings | None
    ) -> None:
        args = profile.qemu.extra_args
        changed = previous is None or settings != previous
        conflicting = {
            "-device",
            "-vga",
            "-audiodev",
            "-audio",
            "-soundhw",
            "-nic",
            "-net",
            "-netdev",
            "-boot",
        }
        if changed and any(argument.split("=", 1)[0] in conflicting for argument in args):
            raise PaletteError(
                ErrorCode.STATE_CONFLICT, "可信 profile 的附加硬件参数与设置冲突; 请先修改 profile"
            )
        if (
            settings.network.model != (previous.network.model if previous else None)
            and profile.network.extra_options
            and "model=" in profile.network.extra_options
        ):
            raise PaletteError(ErrorCode.STATE_CONFLICT, "profile 已指定网卡型号; 不能在设置中覆盖")
        if (
            settings.network.mac != (previous.network.mac if previous else None)
            and profile.network.extra_options
            and "mac=" in profile.network.extra_options
        ):
            raise PaletteError(
                ErrorCode.STATE_CONFLICT, "profile 已指定 MAC 地址; 不能在设置中覆盖"
            )
        managed = (
            [DesktopManager._forward(entry) for entry in previous.network.forwards]
            if previous
            else []
        )
        preserved = [entry for entry in profile.network.hostfwd if entry not in managed]
        listeners = set()
        for entry in preserved:
            match = re.match(r"^(tcp|udp):[^:]*:(\d+)-", entry)
            if match:
                listeners.add((match[1], int(match[2])))
        if any(
            (entry.protocol, entry.host_port) in listeners for entry in settings.network.forwards
        ):
            raise PaletteError(
                ErrorCode.INVALID_ARGUMENT, "新增端口转发与 profile 已有监听端口冲突"
            )

    def _operation_path(self, vm_id: str) -> Path:
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

    async def recover_operations(self) -> None:
        async with self._lock:
            for vm_id in self.definitions:
                try:
                    await self._recover_resize(vm_id)
                except (PaletteError, OSError, ValueError, KeyError):
                    # Keep the journal: this VM cannot start until recovery succeeds.
                    continue

    async def _recover_resize(self, vm_id: str) -> None:
        path = self._operation_path(vm_id)
        if not path.exists():
            return
        self._require_stopped(vm_id)
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
        storage = self._storage(definition)
        if storage is None or record["overlay"] != str(storage.overlay):
            raise PaletteError(ErrorCode.IMAGE_INVALID, "扩容记录与受管理磁盘不一致")
        images = self.service.manager._images(definition.profile)
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
        async with self._lock:
            await self._recover_resize(vm_id)
            self._require_stopped(vm_id)
            definition = self.get(vm_id)
            try:
                request = DiskResize.model_validate(payload)
            except ValidationError as exc:
                raise self._validation_error(exc) from exc
            self._revision(definition, request.revision)
            if request.disk_gib <= definition.draft.disk_gib:
                raise PaletteError(ErrorCode.INVALID_ARGUMENT, "磁盘只能扩容; 不能缩小或保持原容量")
            images = self.service.manager._images(definition.profile)
            storage = self._storage(definition)
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
                    self._operation_path(vm_id),
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
                    await self._recover_resize(vm_id)
                    if code or self.get(vm_id).draft.disk_gib != request.disk_gib:
                        raise PaletteError(
                            ErrorCode.IMAGE_INVALID, "磁盘扩容失败; 请检查当前实盘容量后重试"
                        )

                await finish_cleanup(asyncio.create_task(resize()))
            return {
                "revision": self.get(vm_id).revision,
                "disk_gib": self.get(vm_id).draft.disk_gib,
            }

    def _require_stopped(self, vm_id: str) -> None:
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
            self._storage(definition)

    def _storage(self, definition: VmDefinition) -> InstancePaths | None:
        owner = definition.storage_owner
        if owner is None:
            return None
        images = self.service.manager._images(definition.profile)
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

    async def _seed(self, definition: VmDefinition) -> VmDefinition:
        if definition.draft.source != "iso" or definition.seed_owner is not None:
            return definition
        images = self.service.manager._images(definition.profile)
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

    async def start(self, vm_id: str) -> dict[str, Any]:
        async with self._lock:
            return await self._start(vm_id)

    async def _start(self, vm_id: str) -> dict[str, Any]:
        await self._recover_resize(vm_id)
        definition = await self._seed(self.get(vm_id))
        storage = self._storage(definition)
        settings = self.settings(definition)
        if definition.draft.settings:
            caps = await validate_hardware(self.probe, definition.profile, settings)
            if settings.audio_enabled:
                definition = definition.model_copy(
                    update={
                        "profile": definition.profile.model_copy(
                            update={
                                "audio": definition.profile.audio.model_copy(
                                    update={"backend": caps["audio_backend"]}
                                )
                            }
                        )
                    }
                )
        volumes: tuple[ExtraVolume, ...] = ()
        if settings.iso_path and definition.iso_attached:
            volumes = (
                ExtraVolume(
                    path=str(validate_caller_path(settings.iso_path, what="ISO")),
                    media="cdrom",
                    boot_index=0
                    if settings.boot_order == "cdrom"
                    or (settings.boot_order == "auto" and storage is None)
                    else 2,
                ),
            )
        overrides = LaunchOverrides(volumes=volumes)

        def ready(instance: VmInstance) -> None:
            images = self.service.manager._images(instance.profile)
            images.update_manifest(
                instance.paths,
                {
                    "storage_owner": definition.storage_owner or instance.paths.instance_id,
                    "hardware_vars": str(instance.paths.hardware_vars)
                    if instance.paths.hardware_vars
                    else None,
                },
            )
            if definition.storage_owner is None:
                self._save(
                    definition.model_copy(
                        update={
                            "storage_owner": instance.paths.instance_id,
                            "profile": instance.profile,
                        }
                    )
                )

        instance = await self.service.manager.start(
            vm_id,
            profile_override=definition.profile,
            storage=storage,
            overrides=overrides,
            on_storage_ready=ready,
        )
        return {
            "vm_id": vm_id,
            "instance_id": instance.view.instance_id,
            "state": str(instance.state),
        }

    async def stop(
        self, vm_id: str, *, force: bool = False, expected_instance_id: str | None = None
    ) -> dict[str, Any]:
        async with self._lock:
            instance = self.service.manager.find(vm_id)
            self.service.manager._check_identity(instance, expected_instance_id)
            await self.service.control.revoke(vm_id)
            if instance is not None:
                if force:
                    await self.service.stop(
                        vm_id, force=True, expected_instance_id=expected_instance_id
                    )
                else:
                    await self.service.manager.graceful_stop(
                        vm_id, expected_instance_id=expected_instance_id
                    )
                definition = self.definitions.get(vm_id)
                if definition and definition.storage_owner:
                    owner_paths = self._storage_after_stop(definition)
                    self.service.manager._images(instance.profile).update_manifest(
                        owner_paths, {"state": "STOPPED", "process_exited": True}
                    )
            return {"vm_id": vm_id, "state": "STOPPED"}

    def _storage_after_stop(self, definition: VmDefinition) -> InstancePaths:
        owner = definition.storage_owner
        if owner is None:
            raise PaletteError(ErrorCode.IMAGE_MISSING, "no persistent storage exists")
        root = self.guard.resolve("vms", definition.vm_id, owner)
        return InstancePaths(
            definition.vm_id,
            owner,
            root,
            root / "disk",
            root / "disk" / "overlay.qcow2",
            root / "frames",
            root / "logs",
            root / "manifest.json",
        )

    async def restart(self, vm_id: str) -> dict[str, Any]:
        await self.stop(vm_id)
        return await self.start(vm_id)

    async def reset(self, vm_id: str) -> dict[str, Any]:
        await self.stop(vm_id)
        async with self._lock:
            old = self.get(vm_id)
            previous = (
                self.guard.resolve("vms", vm_id, old.storage_owner)
                if old.storage_owner is not None
                else None
            )
            if previous is not None:
                self._extra_pins.add(previous)
            try:
                self._save(old.model_copy(update={"storage_owner": None}))
                return await self._start(vm_id)
            except BaseException:
                self._save(old)
                raise
            finally:
                if previous is not None:
                    self._extra_pins.discard(previous)
                self._pin()

    async def delete(self, vm_id: str) -> dict[str, Any]:
        async with self._lock:
            definition = self.get(vm_id)
            self._require_stopped(vm_id)
            self.guard.resolve("definitions", vm_id + ".json").unlink()
            self._operation_path(vm_id).unlink(missing_ok=True)
            self.definitions.pop(vm_id)
            self.service.manager._instances.pop(vm_id, None)
            self._pin()
            images = ImageManager(
                self.guard, definition.profile, keep_overlays=False, max_overlays=0
            )
            images.prune_instances(
                vm_id,
                keep_instance_ids=set(),
                protected_paths=tuple(self.service.manager.persistent_roots),
            )
            return {"deleted": vm_id}
