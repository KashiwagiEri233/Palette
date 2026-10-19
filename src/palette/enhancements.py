"""Operator enhancement routes and per-feature availability; unavailable is not uninstalled."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from .config import SharedFolderConfig
from .errors import ErrorCode, PaletteError

if TYPE_CHECKING:
    from .service import PaletteService
    from .vm.manager import VmInstance


class EnhancementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    instance_id: str = Field(min_length=1, max_length=128)
    lease_id: str | None = None
    text: str | None = Field(default=None, max_length=49152)
    width: int | None = None
    height: int | None = None
    path: str | None = Field(default=None, max_length=4096)
    mode: str | None = None
    transfer_id: str | None = None
    count: int = 49152
    data_base64: str | None = Field(default=None, max_length=65540)
    folders: list[SharedFolderConfig] | None = Field(default=None, max_length=16)


def enhancement_status(instance: VmInstance) -> dict[str, Any]:
    agent = instance.qemu_guest_agent
    desktop = instance.guest_desktop
    ports = instance.enhancement_ports
    clipboard = bool(
        instance.plan
        and instance.plan.clipboard_enabled
        and ports.get("palette-vdagent")
        and desktop
        and desktop.connected
    )
    resize = bool(
        instance.profile.guest_desktop.resize
        and instance.profile.devices.display_model in {"virtio-gpu-pci", "virtio-vga"}
        and ports.get("palette-session")
        and instance.guest_session
        and instance.guest_session.online
        and desktop
        and desktop.connected
    )
    shares = instance.guest_shares
    return {
        "qga": agent.status()
        if agent
        else {
            "enabled": instance.profile.guest_agent.enabled,
            "online": False,
            "commands": [],
            "detail": "该配置未启用 QEMU Guest Agent",
        },
        "desktop": {
            "clipboard": clipboard,
            "resize": resize,
            "reason": "不可用的增强功能保留基础输入和画面缩放; 请检查客户机工具、桌面会话及 virtio-gpu 显卡"
            if not clipboard or not resize
            else "桌面增强可用",
        },
        "shares": shares.status(bool(ports.get("palette-webdav")))
        if shares
        else {"available": False, "folders": [], "url": "dav://127.0.0.1:9843/"},
    }


class EnhancementRoutes:
    def __init__(self, service: PaletteService) -> None:
        self.service = service

    async def route(self, vm_id: str, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = EnhancementRequest.model_validate(payload)
        manager = self.service.manager
        async with manager.operations.hold(vm_id):
            instance = await manager.require_running(vm_id)
            self.service.require_instance_id(instance, request.instance_id)
            if method == "guest.status":
                return enhancement_status(instance)
            if request.lease_id is None:
                raise PaletteError(ErrorCode.CONTROL_NOT_OWNED, "请先接管虚拟机")
            lease = self.service.control.validate(vm_id, request.lease_id)

            def check() -> None:
                self.service.control.validate(vm_id, request.lease_id or "")
                self.service.require_instance_id(instance, request.instance_id)

            async def run() -> dict[str, Any]:
                status = enhancement_status(instance)
                desktop = instance.guest_desktop
                if method.startswith("control.clipboard."):
                    if not status["desktop"]["clipboard"] or desktop is None:
                        raise PaletteError(
                            ErrorCode.GUEST_TOOL_UNAVAILABLE,
                            "客户机剪贴板服务尚未就绪, 基础键盘可用",
                        )
                    if method == "control.clipboard.get":
                        return await desktop.clipboard_get()
                    if request.text is None:
                        raise PaletteError(ErrorCode.INVALID_ARGUMENT, "缺少剪贴板文本")
                    return await desktop.clipboard_set(request.text)
                if method == "control.display.resize":
                    if not status["desktop"]["resize"] or desktop is None:
                        raise PaletteError(
                            ErrorCode.GUEST_TOOL_UNAVAILABLE, "自动分辨率不可用, 仍可缩放画面"
                        )
                    return await desktop.resize(request.width or 0, request.height or 0)
                if method == "guest.share.set":
                    shares = instance.guest_shares
                    if shares is None:
                        raise PaletteError(
                            ErrorCode.UNSUPPORTED_CAPABILITY, "配置未启用共享目录通道, 请重启后重试"
                        )
                    folders = request.folders or []
                    if len({item.tag for item in folders}) != len(folders):
                        raise PaletteError(ErrorCode.INVALID_ARGUMENT, "共享目录名称重复")
                    async with shares.lock:
                        shares.configure(folders)
                    return shares.status(bool(instance.enhancement_ports.get("palette-webdav")))
                agent = instance.qemu_guest_agent
                if agent is None:
                    raise PaletteError(ErrorCode.GUEST_TOOL_UNAVAILABLE, "QEMU Guest Agent 不可用")
                if method == "guest.sync_time":
                    return await agent.sync_time()
                if method == "guest.file.open":
                    return await agent.file_open(request.path or "", request.mode or "")
                if method == "guest.file.read":
                    return await agent.file_read(request.transfer_id or "", request.count)
                if method == "guest.file.write":
                    return await agent.file_write(
                        request.transfer_id or "", request.data_base64 or ""
                    )
                if method == "guest.file.close":
                    return await agent.file_close(request.transfer_id or "")
                raise PaletteError(ErrorCode.INVALID_ARGUMENT, "未知增强操作")

            async with lease.lock:
                check()
                return await self.service.controller_for(instance).operator_enhancement(
                    method, check, run
                )
