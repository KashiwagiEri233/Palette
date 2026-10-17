"""Persistent VM definitions and their atomic on-disk representation."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .config import Profile
from .errors import ErrorCode, PaletteError
from .policy import PathGuard, validate_identifier
from .vm.manager import VmManager
from .vm_settings import HardwareSettings


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


class DefinitionRepository:
    def __init__(self, manager: VmManager) -> None:
        self.manager = manager
        self.guard = PathGuard(manager.config.runtime_root)
        self.directory = self.guard.resolve("definitions")
        self.definitions: dict[str, VmDefinition] = {}
        self.extra_pins: set[Path] = set()
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
        self.pin()

    def pin(self) -> None:
        roots = self.extra_pins | {
            self.guard.resolve("vms", definition.vm_id, owner)
            for definition in self.definitions.values()
            for owner in (definition.storage_owner, definition.seed_owner)
            if owner is not None
        }
        self.manager.persistent_roots.clear()
        self.manager.persistent_roots.update(roots)

    def save(self, definition: VmDefinition) -> None:
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
        self.pin()

    def get(self, vm_id: str) -> VmDefinition:
        validate_identifier(vm_id, what="vm_id")
        try:
            return self.definitions[vm_id]
        except KeyError as exc:
            raise PaletteError(ErrorCode.VM_NOT_FOUND, "VM definition not found") from exc
