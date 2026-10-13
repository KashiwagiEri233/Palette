"""Persistent operator-owned VM definitions; runtime instances remain disposable."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .config import Profile
from .errors import ErrorCode, PaletteError
from .policy import PathGuard, validate_identifier
from .vm.images import ImageManager, InstancePaths, validate_caller_path
from .vm.launcher import ExtraVolume, LaunchOverrides, run_tool
from .vm.manager import VmInstance, VmState

if TYPE_CHECKING:
    from .service import PaletteService


class VmDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    name: str = Field(min_length=1, max_length=120)
    profile_id: str
    source: Literal["image", "iso"]
    path: str = Field(min_length=1, max_length=4096)
    image_format: Literal["qcow2", "raw"] = "qcow2"
    vcpus: int = Field(ge=1, le=64)
    memory_mib: int = Field(ge=256, le=1048576)
    disk_gib: int = Field(default=24, ge=1, le=4096)
    network: Literal["offline", "restricted-lab", "nat"] = "restricted-lab"


class VmDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
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
            draft = VmDraft.model_validate(payload)
            path = validate_caller_path(draft.path, what=draft.source)
            profile = self.service.config.profile(draft.profile_id)
            data = profile.model_dump()
            data["resources"]["vcpus"] = draft.vcpus
            data["resources"]["memory_mib"] = draft.memory_mib
            if data["resources"]["process_memory_limit_mib"] is not None:
                data["resources"]["process_memory_limit_mib"] = max(
                    draft.memory_mib + 2048, data["resources"]["process_memory_limit_mib"]
                )
            data["network"]["mode"] = draft.network
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
            )
            self._save(definition)
            return definition.model_dump(mode="json")

    async def update(self, vm_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            definition = self.get(vm_id)
            self._require_stopped(vm_id)
            if set(payload) - {"name", "vcpus", "memory_mib", "network", "iso_attached"}:
                raise PaletteError(
                    ErrorCode.INVALID_ARGUMENT, "storage and architecture cannot be edited"
                )
            data = definition.draft.model_dump()
            data.update({key: value for key, value in payload.items() if key != "iso_attached"})
            draft = VmDraft.model_validate(data)
            profile_data = definition.profile.model_dump()
            profile_data["resources"].update(vcpus=draft.vcpus, memory_mib=draft.memory_mib)
            limit = profile_data["resources"]["process_memory_limit_mib"]
            if limit is not None:
                profile_data["resources"]["process_memory_limit_mib"] = max(
                    limit, draft.memory_mib + 2048
                )
            profile_data["network"]["mode"] = draft.network
            profile_data["images"]["size_gib"] = draft.disk_gib
            profile = Profile.model_validate(profile_data)
            if "iso_attached" in payload and not isinstance(payload["iso_attached"], bool):
                raise PaletteError(ErrorCode.INVALID_ARGUMENT, "iso_attached must be boolean")
            definition = definition.model_copy(
                update={
                    "draft": draft,
                    "profile": profile,
                    "iso_attached": payload.get("iso_attached", definition.iso_attached),
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            )
            self._save(definition)
            return definition.model_dump(mode="json")

    def _require_stopped(self, vm_id: str) -> None:
        instance = self.service.manager.find(vm_id)
        if instance is not None and (
            instance.state not in {VmState.STOPPED, VmState.FAILED}
            or (instance.process is not None and instance.process.returncode is None)
        ):
            raise PaletteError(
                ErrorCode.STATE_CONFLICT, "shut down the VM before editing or deleting it"
            )

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
        definition = await self._seed(self.get(vm_id))
        storage = self._storage(definition)
        volumes: tuple[ExtraVolume, ...] = ()
        if definition.draft.source == "iso" and definition.iso_attached:
            volumes = (
                ExtraVolume(
                    path=str(validate_caller_path(definition.draft.path, what="ISO")),
                    media="cdrom",
                    boot_index=0 if storage is None else 2,
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

    async def stop(self, vm_id: str, *, force: bool = False) -> dict[str, Any]:
        async with self._lock:
            await self.service.control.revoke(vm_id)
            instance = self.service.manager.find(vm_id)
            if instance is not None:
                if force:
                    await self.service.stop(vm_id, force=True)
                else:
                    await self.service.manager.graceful_stop(vm_id)
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
