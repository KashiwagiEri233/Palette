"""Verified, operator-mounted Windows installation media on a dedicated optical drive."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .config import PaletteConfig, Profile
from .errors import ErrorCode, PaletteError
from .policy import Policy
from .vm.manager import VmState

if TYPE_CHECKING:
    from .desktop import DesktopManager


class ToolsManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1]
    guest_os: Literal["windows"]
    arch: Literal["x86_64"]
    version: str = Field(min_length=1, max_length=64)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=32768, le=2 * 1024**3)


class MediaRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: int = Field(ge=0)
    instance_id: str | None = Field(default=None, min_length=1, max_length=128)


def _checked(config: PaletteConfig) -> tuple[Path, str]:
    directory = config.config_root / "images" / "guest-tools"
    path = directory / "palette-tools-windows-x86_64.iso"
    manifest_path = path.with_suffix(".json")
    try:
        if manifest_path.stat().st_size > 16384:
            raise ValueError("manifest too large")
        manifest = ToolsManifest.model_validate_json(manifest_path.read_bytes())
        with path.open("rb") as handle:
            if path.stat().st_size != manifest.size_bytes:
                raise ValueError("size mismatch")
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        if digest != manifest.sha256:
            raise ValueError("checksum mismatch")
    except (OSError, ValueError, ValidationError) as exc:
        raise PaletteError(
            ErrorCode.IMAGE_INVALID,
            "增强工具 ISO 缺失或校验失败, 请先运行 uv run python scripts/build_windows_tools.py",
        ) from exc
    return path.resolve(), digest


async def checked_tools_iso(config: PaletteConfig, profile: Profile) -> tuple[Path, str]:
    if profile.qemu.arch != "x86_64":
        raise PaletteError(
            ErrorCode.UNSUPPORTED_CAPABILITY,
            "当前 Windows 工具盘仅支持 x86_64, 不能在 ARM64 客户机中安装这些驱动",
        )
    return await asyncio.to_thread(_checked, config)


async def change_tools_media(
    desktop: DesktopManager, vm_id: str, payload: dict[str, Any], *, mount: bool
) -> dict[str, Any]:
    try:
        request = MediaRequest.model_validate(payload)
    except ValidationError as exc:
        raise PaletteError(ErrorCode.INVALID_ARGUMENT, "工具盘操作参数无效") from exc
    service = desktop.service
    async with service.manager.operations.hold(vm_id):
        definition = desktop.get(vm_id)
        desktop._revision(definition, request.revision)
        service.driver.require(vm_id)
        Policy(definition.profile, desktop.guard).require_host_material("iso", actor="operator")
        if mount and definition.draft.os_preset == "linux":
            raise PaletteError(
                ErrorCode.UNSUPPORTED_CAPABILITY, "Windows 工具盘不适用于 Linux 预设"
            )
        path, digest = (
            await checked_tools_iso(service.config, definition.profile) if mount else (None, None)
        )
        instance = service.manager.find(vm_id)
        live = desktop._running(vm_id)
        if live:
            if instance is None or instance.state != VmState.RUNNING or instance.qmp is None:
                raise PaletteError(ErrorCode.STATE_CONFLICT, "请等待虚拟机进入运行状态后装载工具盘")
            if request.instance_id is None:
                raise PaletteError(ErrorCode.INVALID_ARGUMENT, "运行中装载需要当前实例 ID")
            service.require_instance_id(instance, request.instance_id)
            blocks = await instance.qmp.execute("query-block")
            drive = next((item for item in blocks if item.get("device") == "palette-tools"), None)
            if drive is None:
                raise PaletteError(
                    ErrorCode.STATE_CONFLICT, "当前实例没有工具光驱, 请关闭并重新启动虚拟机"
                )
            arguments: dict[str, Any] = (
                {"id": drive["qdev"]} if drive.get("qdev") else {"device": "palette-tools"}
            )
            if mount:
                arguments.update(
                    filename=str(path), format="raw", **{"read-only-mode": "read-only"}
                )
                await instance.qmp.execute("blockdev-change-medium", arguments)
            else:
                arguments["force"] = False
                await instance.qmp.execute("eject", arguments)
            observed = await instance.qmp.execute("query-block")
            after: dict[str, Any] = next(
                (item for item in observed if item.get("device") == "palette-tools"), {}
            )
            inserted = after.get("inserted")
            confirmed = (
                bool(
                    inserted
                    and inserted.get("ro")
                    and Path(inserted.get("file", "")).resolve() == path
                )
                if mount
                else bool(after) and not inserted
            )
            if not confirmed:
                raise PaletteError(
                    ErrorCode.ACTION_OUTCOME_UNKNOWN, "工具盘状态尚未确认, 请检查客户机光驱后重试"
                )
        elif request.instance_id is not None:
            raise PaletteError(ErrorCode.STALE_FRAME, "虚拟机运行实例已改变, 请刷新后重试")
        updated = definition.model_copy(
            update={
                "tools_iso_sha256": digest,
                "revision": definition.revision + 1,
                "updated_at": datetime.now(UTC).isoformat(),
            }
        )
        try:
            desktop._save(updated)
        except OSError as exc:
            raise PaletteError(
                ErrorCode.INTERNAL_ERROR, "工具盘状态保存失败; 运行中介质可能已改变, 请检查后重试"
            ) from exc
        service.record_event("vm.tools_media", vm_id, attached=mount, live=live)
        return {"attached": mount, "live": live, "revision": updated.revision}
