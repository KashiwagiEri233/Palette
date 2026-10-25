"""Persistent operator-owned VM definitions; runtime instances remain disposable."""

from __future__ import annotations

import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psutil
from pydantic import ValidationError

from .config import Profile
from .definitions import DefinitionRepository, VmDefinition, VmDraft, VmUpdate
from .errors import ErrorCode, PaletteError
from .managed_drives import managed_disk_path, managed_drive_path, save_with_managed_drives
from .persistent_storage import PersistentStorage
from .primary_drive import PRIMARY_DRIVE_ID, include_primary, primary_present
from .vm.images import InstancePaths, validate_caller_path
from .vm.launcher import ExtraVolume, LaunchOverrides
from .vm.manager import ShutdownMode, VmInstance, VmState
from .vm_settings import (
    HardwareSettings,
    apply_settings,
    normalize_optical,
    settings_from_profile,
    validate_hardware,
)

if TYPE_CHECKING:
    from .service import PaletteService


def _disk_bytes(path: Path) -> int | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return getattr(stat, "st_blocks", stat.st_size // 512) * 512


class DesktopManager:
    def __init__(self, service: PaletteService) -> None:
        self.service = service
        self.repository = DefinitionRepository(service.manager)
        self.guard = self.repository.guard
        self.directory = self.repository.directory
        self.definitions = self.repository.definitions
        self._extra_pins = self.repository.extra_pins
        self.storage = PersistentStorage(service, self.repository)
        self.probe = service.manager.probe
        service.lifecycle.desktop = self

    def _pin(self) -> None:
        self.repository.pin()

    def _save(self, definition: VmDefinition) -> None:
        self.repository.save(definition)

    def get(self, vm_id: str) -> VmDefinition:
        return self.repository.get(vm_id)

    async def list(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for vm_id in sorted(set(self.definitions) | set(self.service.manager.known())):
            definition = self.definitions.get(vm_id)
            instance = self.service.manager.find(vm_id)
            status = await self.service.status(vm_id, actor="operator")
            result.append(
                {
                    "vm_id": vm_id,
                    "name": definition.draft.name if definition else vm_id,
                    "persistent": definition is not None,
                    "definition": {
                        "draft": definition.draft.model_copy(
                            update={"settings": self.settings(definition)}
                        ).model_dump(mode="json"),
                        "iso_attached": definition.iso_attached,
                        "revision": definition.revision,
                        "tools_iso_attached": definition.tools_iso_sha256 is not None,
                    }
                    if definition
                    else None,
                    "status": {
                        **status.status.model_dump(mode="json"),
                        "process_running": bool(
                            instance and instance.process and instance.process.returncode is None
                        ),
                    }
                    if status.status
                    else {
                        "state": "STOPPED",
                        "process_running": False,
                        "vm_id": vm_id,
                        "guest_arch": definition.profile.qemu.arch if definition else None,
                    },
                }
            )
        return result

    async def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        vm_id = "vm_" + uuid.uuid4().hex[:12]
        async with self.service.manager.operations.hold(vm_id):
            try:
                draft = VmDraft.model_validate(payload)
            except ValidationError as exc:
                raise self._validation_error(exc) from exc
            path = validate_caller_path(draft.path, what=draft.source)
            profile = self.service.config.profile(draft.profile_id)
            if draft.os_preset != "custom":
                from .platforms import validate_target

                validate_target(profile)
            cores = os.cpu_count()
            if (cores and draft.vcpus > cores) or (
                draft.memory_mib > psutil.virtual_memory().total // (1024 * 1024)
            ):
                raise PaletteError(ErrorCode.INVALID_ARGUMENT, "CPU 或内存超过宿主资源上限")
            if draft.source == "image":
                info = await self.service.manager.images_for(profile).inspect_image(
                    path, image_format=draft.image_format
                )
                if draft.disk_gib * 1024**3 < info.virtual_size:
                    raise PaletteError(ErrorCode.INVALID_ARGUMENT, "磁盘容量不能小于镜像虚拟容量")
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
                vm_id=vm_id,
                draft=draft.model_copy(update={"path": str(path)}),
                profile=Profile.model_validate(data),
                created_at=now,
                updated_at=now,
                iso_attached=draft.iso_attached,
            )
            settings = self.settings(definition)
            try:
                settings = HardwareSettings.model_validate(settings.model_dump())
            except ValidationError as exc:
                raise self._validation_error(exc) from exc
            definition = definition.model_copy(
                update={
                    "draft": definition.draft.model_copy(
                        update={
                            "settings": settings
                            if definition.draft.settings or settings.drives
                            else None,
                            "iso_attached": False,
                        }
                    ),
                    "iso_attached": False,
                    "drives_initialized": True,
                }
            )
            self._validate_media(settings)
            await self._validate_drives(definition.profile, settings, definition)
            definition = await save_with_managed_drives(self, definition)
            return definition.model_dump(mode="json")

    async def update(self, vm_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with self.service.manager.operations.hold(vm_id):
            await self.storage.recover_resize(vm_id)
            definition = self.get(vm_id)
            self.storage.require_stopped(vm_id)
            try:
                update = VmUpdate.model_validate(payload)
            except ValidationError as exc:
                raise self._validation_error(exc) from exc
            self._revision(definition, update.revision)
            update = update.model_copy(
                update={
                    "settings": normalize_optical(update.settings, attached=update.iso_attached)
                }
            )
            self._resource_conflicts(definition.profile, update.vcpus, update.memory_mib)
            previous = self.settings(definition)
            await validate_hardware(self.probe, definition.profile, update.settings, previous)
            self._check_conflicts(definition.profile, update.settings, previous)
            self._validate_media(update.settings)
            await self._validate_drives(definition.profile, update.settings, definition)
            data = definition.draft.model_dump()
            data.update(update.model_dump(exclude={"revision", "iso_attached", "delete_drives"}))
            data["iso_attached"] = False
            primary = next(
                (drive for drive in update.settings.drives if drive.id == PRIMARY_DRIVE_ID), None
            )
            if primary is not None:
                if not primary_present(definition):
                    raise PaletteError(ErrorCode.INVALID_ARGUMENT, "已删除的磁盘不能自动恢复")
                if definition.storage_owner is None and primary.size_gib is not None:
                    data["disk_gib"] = primary.size_gib
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
                    "iso_attached": False,
                    "updated_at": datetime.now(UTC).isoformat(),
                    "drives_initialized": True,
                }
            )
            removed = {drive.id for drive in previous.drives} - {
                drive.id for drive in update.settings.drives
            }
            delete_ids = (removed & definition.data_disks.keys()) | set(update.delete_drives)
            if PRIMARY_DRIVE_ID in removed:
                delete_ids.add(PRIMARY_DRIVE_ID)
            definition = await save_with_managed_drives(self, definition, delete_ids=delete_ids)
            return {
                **definition.model_dump(mode="json"),
                "pending_disk_deletions": [
                    key for key, disk in definition.data_disks.items() if disk.deleting
                ],
            }

    def settings(self, definition: VmDefinition) -> HardwareSettings:
        if definition.draft.settings:
            return include_primary(
                definition,
                normalize_optical(definition.draft.settings, attached=definition.iso_attached),
            )
        settings = settings_from_profile(
            definition.profile, definition.draft.path if definition.draft.source == "iso" else None
        )
        if settings.drives and not definition.iso_attached:
            settings.drives[0].attached = False
        return include_primary(definition, settings)

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
        async with self.service.manager.operations.hold(vm_id):
            await self.storage.recover_resize(vm_id)
            definition = self.get(vm_id)
            running = self._running(vm_id)
            disk: dict[str, Any] = {
                "configured_gib": definition.draft.disk_gib if primary_present(definition) else 0,
                "virtual_bytes": None,
                "actual_bytes": None,
                "allocated": definition.storage_owner is not None and primary_present(definition),
            }
            if definition.storage_owner and primary_present(definition):
                images = self.service.manager.images_for(definition.profile)
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
                    stopped_storage = self.storage.paths(definition)
                    if stopped_storage:
                        info = await images.inspect_image(
                            stopped_storage.overlay, image_format="qcow2"
                        )
                        disk.update(virtual_bytes=info.virtual_size, actual_bytes=info.actual_size)
            managed = {
                key: {
                    "size_gib": record.size_gib,
                    "path": str(managed_disk_path(self, vm_id, record)),
                    "deleting": record.deleting,
                    "format": record.format,
                    "virtual_bytes": record.virtual_size or record.size_gib * 1024**3,
                    "source_name": record.source_name,
                    "actual_bytes": _disk_bytes(managed_disk_path(self, vm_id, record)),
                }
                for key, record in definition.data_disks.items()
            }
            if primary_present(definition):
                managed[PRIMARY_DRIVE_ID] = {
                    "size_gib": definition.draft.disk_gib,
                    "path": str(self._storage_after_stop(definition).overlay)
                    if definition.storage_owner
                    else None,
                    "allocated": definition.storage_owner is not None,
                    "deleting": False,
                    "format": "qcow2",
                    "virtual_bytes": disk["virtual_bytes"] or definition.draft.disk_gib * 1024**3,
                    "actual_bytes": disk["actual_bytes"],
                    "source_name": Path(definition.draft.path).name
                    if definition.draft.source == "image"
                    else None,
                }
            return {
                "revision": definition.revision,
                "draft": definition.draft.model_dump(mode="json"),
                "settings": self.settings(definition).model_dump(mode="json"),
                "managed_disks": managed,
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
    def _validate_media(settings: HardwareSettings) -> None:
        optical = [
            drive
            for drive in settings.drives
            if drive.media == "cdrom" and drive.attached and drive.path
        ]
        if settings.boot_order == "cdrom" and not optical:
            raise PaletteError(ErrorCode.INVALID_ARGUMENT, "光盘优先需要挂载 ISO")
        if settings.boot_drive_id and not any(
            drive.id == settings.boot_drive_id for drive in optical
        ):
            raise PaletteError(ErrorCode.INVALID_ARGUMENT, "启动光驱不存在或没有可用介质")

    async def _validate_drives(
        self, profile: Profile, settings: HardwareSettings, definition: VmDefinition
    ) -> None:
        chains: list[tuple[set[Path], Path | None]] = []
        for drive in settings.drives:
            path: Path | None = None
            if drive.id == PRIMARY_DRIVE_ID:
                if definition.storage_owner:
                    path = self._storage_after_stop(definition).overlay
            elif drive.size_gib is not None:
                if drive.id not in definition.data_disks:
                    continue
                path = managed_drive_path(self, definition, drive)
            else:
                path = Path(drive.path) if drive.path else None
            if path is None or not drive.attached:
                continue
            path = validate_caller_path(path, what="附加驱动器")
            chain = {path}
            if drive.media == "disk":
                info = await self.service.manager.images_for(profile).inspect_image(path)
                if info.format != drive.format and not drive.import_copy:
                    raise PaletteError(ErrorCode.IMAGE_INVALID, "附加磁盘镜像格式与文件不一致")
                if drive.import_copy:
                    # Conversion finishes before launch and produces an independent
                    # image, so the source is not part of the attached writer graph.
                    continue
                chain = set(info.chain)
                if (
                    not drive.read_only
                    and not drive.import_copy
                    and Path(profile.images.base).is_file()
                ):
                    base = await self.service.manager.images_for(profile).inspect_image(
                        validate_caller_path(profile.images.base, what="基础镜像")
                    )
                    if path in base.chain:
                        raise PaletteError(ErrorCode.INVALID_ARGUMENT, "附加磁盘不能写入基础镜像链")
            writer = None if drive.read_only or drive.import_copy else path
            if any(
                (writer is not None and writer in other)
                or (other_writer is not None and other_writer in chain)
                for other, other_writer in chains
            ):
                raise PaletteError(ErrorCode.INVALID_ARGUMENT, "读写磁盘不能重复挂载同一镜像")
            chains.append((chain, writer))

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

    async def start(self, vm_id: str) -> dict[str, Any]:
        async with self.service.manager.operations.hold(vm_id, starting=True):
            return await self._start(vm_id)

    async def _start(self, vm_id: str) -> dict[str, Any]:
        definition = self.get(vm_id)
        tools_iso = None
        if definition.tools_iso_sha256 is not None:
            from .tools_media import checked_tools_iso

            tools_iso, digest = await checked_tools_iso(self.service.config, definition.profile)
            if digest != definition.tools_iso_sha256:
                raise PaletteError(ErrorCode.STATE_CONFLICT, "增强工具镜像已更新, 请重新装载后启动")
        await self.storage.recover_resize(vm_id)
        definition = await self.storage.seed(self.get(vm_id))
        storage = self.storage.paths(definition)
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
        self._validate_media(settings)
        await self._validate_drives(definition.profile, settings, definition)
        optical = [
            drive
            for drive in settings.drives
            if drive.media == "cdrom" and drive.attached and drive.path
        ]
        boot_drive = settings.boot_drive_id or (optical[0].id if optical else None)
        optical_first = settings.boot_order == "cdrom" or (
            settings.boot_order == "auto" and storage is None
        )
        disks = [drive.id for drive in settings.drives if drive.media == "disk" and drive.attached]
        optical_ids = [drive.id for drive in optical if drive.id == boot_drive] + [
            drive.id for drive in optical if drive.id != boot_drive
        ]
        boot_ids = optical_ids + disks if optical_first else disks + optical_ids
        boot_ranks = {drive_id: index for index, drive_id in enumerate(boot_ids)}
        volumes = tuple(
            ExtraVolume(
                path=str(
                    managed_drive_path(self, definition, drive)
                    if drive.size_gib is not None
                    else validate_caller_path(drive.path or "", what="附加驱动器")
                )
                if drive.id != PRIMARY_DRIVE_ID
                and (drive.path or drive.size_gib is not None)
                and drive.attached
                else None,
                media=drive.media,
                format=drive.format,
                read_only=drive.read_only,
                device_id=drive.id,
                interface=drive.interface,
                boot_index=boot_ranks.get(drive.id),
                use_overlay=drive.id == PRIMARY_DRIVE_ID,
            )
            for drive in settings.drives
            if drive.media == "cdrom" or drive.attached
        )
        overrides = LaunchOverrides(
            volumes=volumes,
            tools_iso=tools_iso,
            attach_primary=False,
            create_overlay=primary_present(definition),
        )

        def ready(instance: VmInstance) -> None:
            images = self.service.manager.images_for(instance.profile)
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
        manager = self.service.manager
        authorized = manager.find(vm_id)
        expected = expected_instance_id or (authorized.view.instance_id if authorized else None)
        if force:
            manager.escalate_shutdown(vm_id, expected_instance_id=expected)
        async with manager.operations.hold(vm_id):
            instance = self.service.manager.find(vm_id)
            manager.check_identity(instance, expected, expect_absent=authorized is None)
            if instance is not None:
                instance.shutdown_in_progress = True
                try:
                    if not force:
                        try:
                            await self.service.control.revoke(vm_id)
                        except PaletteError as exc:
                            if not instance.force_shutdown.is_set():
                                raise PaletteError(
                                    exc.code,
                                    exc.message,
                                    details={
                                        **exc.details,
                                        "operation": "guest-shutdown",
                                        "vm_id": vm_id,
                                        "instance_id": instance.view.instance_id,
                                    },
                                ) from exc
                    await manager.shutdown_instance(
                        vm_id,
                        mode=ShutdownMode.FORCE
                        if force or instance.force_shutdown.is_set()
                        else ShutdownMode.GRACEFUL,
                        expected_instance_id=expected,
                    )
                    if force:
                        await self.service.control.revoke(vm_id)
                finally:
                    instance.shutdown_in_progress = False
                definition = self.definitions.get(vm_id)
                if definition and definition.storage_owner:
                    owner_paths = self._storage_after_stop(definition)
                    self.service.manager.images_for(instance.profile).update_manifest(
                        owner_paths, {"state": "STOPPED", "process_exited": True}
                    )
            else:
                await self.service.control.revoke(vm_id)
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
        async with self.service.manager.operations.hold(vm_id, starting=True):
            await self._stop_before_rebuild(vm_id)
            return await self.start(vm_id)

    async def _stop_before_rebuild(self, vm_id: str) -> None:
        previous = self.service.manager.find(vm_id)
        was_live = bool(previous and previous.process and previous.process.returncode is None)
        await self.stop(vm_id)
        if was_live and previous is not None and previous.force_shutdown.is_set():
            raise PaletteError(ErrorCode.STATE_CONFLICT, "rebuild cancelled by force stop")

    async def reset(self, vm_id: str) -> dict[str, Any]:
        async with self.service.manager.operations.hold(vm_id, starting=True):
            await self._stop_before_rebuild(vm_id)
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
        async with self.service.manager.operations.hold(vm_id):
            definition = self.get(vm_id)
            self.storage.require_stopped(vm_id)
            self.guard.resolve("definitions", vm_id + ".json").unlink()
            self.storage.operation_path(vm_id).unlink(missing_ok=True)
            self.definitions.pop(vm_id)
            self.service.manager.forget(vm_id)
            self._pin()
            self.service.manager.storage.prune(
                vm_id,
                definition.profile,
                [self.service.manager.get(known) for known in self.service.manager.known()],
                delete_all=True,
            )
            return {"deleted": vm_id}

    async def recover_operations(self) -> None:
        for vm_id in list(self.definitions):
            async with self.service.manager.operations.hold(vm_id):
                try:
                    await self.storage.recover_resize(vm_id)
                except (PaletteError, OSError, ValueError, KeyError):
                    continue

    async def resize_disk(self, vm_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.storage.resize_disk(vm_id, payload)
