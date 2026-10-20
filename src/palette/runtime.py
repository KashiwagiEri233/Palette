"""One runtime owner, shared by a daemon or a standalone MCP session."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any

from . import __version__
from .cleanup import close_all, join_cleanup
from .desktop import DesktopManager
from .display import DisplayGateway
from .enhancements import EnhancementRoutes
from .errors import ErrorCode, PaletteError
from .operator.protocol import DesktopCommand
from .operator.server import OperatorServer
from .service import PaletteService

if TYPE_CHECKING:
    from .agent_ipc import AgentServer


class RuntimeOwner:
    def __init__(self, service: PaletteService, *, kind: str = "embedded") -> None:
        self.service = service
        self.kind = kind
        self.operator = OperatorServer(service)
        self.desktop = DesktopManager(service)
        self.display = DisplayGateway(service)
        self.enhancements = EnhancementRoutes(service)
        self.stopping = asyncio.Event()
        self.operator.desktop_route = self.route
        self.agent: AgentServer | None = None
        self._close_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        from .agent_ipc import AgentServer

        self.operator.acquire()
        try:
            await self.service.startup()
            await self.desktop.recover_operations()
            await self.display.start()
            self.service.manager.console_enabled = True
            self.agent = AgentServer(self.service, self.operator)
            await self.agent.start()
            await self.operator.start()
        except BaseException:
            try:
                await self.close()
            except BaseException as exc:
                logging.getLogger("palette.runtime").error(
                    "startup cleanup failed: %s", type(exc).__name__
                )
            raise

    async def close(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close(), name="runtime-close")
        await join_cleanup(self._close_task)

    async def _close(self) -> None:
        try:
            steps = [self.service.manager.operations.seal, self.operator.stop]
            if self.agent is not None:
                steps.append(self.agent.stop)
            steps.extend((self.display.close, self.service.shutdown))
            await close_all(steps)
        finally:
            self.operator.release()

    async def route(self, command: DesktopCommand) -> dict[str, Any]:
        method, payload = command.method, command.payload
        vm_id = command.vm_id or ""
        if method == "hello":
            if self.agent is None:
                raise PaletteError(ErrorCode.STATE_CONFLICT, "the runtime is not started")
            return {
                "version": __version__,
                "desktop_protocol": 1,
                "features": [
                    "vm-settings-v2",
                    "os-presets-v1",
                    "disk-resize",
                    "runtime-stop",
                    "vm-stop-identity",
                    "driver-claim-v1",
                    "guest-enhancements-v1",
                ],
                "owner_kind": self.kind,
                "pid": os.getpid(),
                "generation": self.operator.endpoint.generation,
                "runtime_root": str(self.service.config.runtime_root),
                "config_root": str(self.service.config.config_root),
                "agent_endpoint": self.agent.endpoint.model_dump(mode="json"),
            }
        if method == "os_presets.list":
            from .os_presets import list_os_presets

            return await list_os_presets(self.service.config, self.desktop.probe)
        if method == "profiles":
            from .platforms import native_profile_id

            return {
                "default_profile": native_profile_id()
                if self.service.config.host.default_profile == "auto"
                else self.service.config.host.default_profile,
                "profiles": [
                    profile.model_dump(mode="json")
                    for profile in self.service.config.profiles.values()
                ],
            }
        if method.startswith("guest.") or method in {
            "control.clipboard.get",
            "control.clipboard.set",
            "control.display.resize",
        }:
            return await self.enhancements.route(vm_id, method, payload)
        if method == "doctor":
            from .cli import build_doctor_report

            return await asyncio.to_thread(
                build_doctor_report, self.service.config, profile_id=payload.get("profile_id")
            )
        if method == "image.info":
            from .vm.images import validate_caller_path

            profile = self.service.config.profile(payload.get("profile_id"))
            path = validate_caller_path(payload["path"], what="image")
            info = await self.service.manager.images_for(profile).inspect_image(path)
            return {"format": info.format, "virtual_size": info.virtual_size}
        if method == "vms.list":
            return {"vms": await self.desktop.list()}
        if method == "vms.create":
            return await self.desktop.create(payload)
        if method in {"vms.tools.mount", "vms.tools.eject"}:
            from .tools_media import change_tools_media

            return await change_tools_media(
                self.desktop, vm_id, payload, mount=method.endswith("mount")
            )
        if method == "vms.update":
            return await self.desktop.update(vm_id, payload)
        if method == "vms.settings.get":
            return await self.desktop.get_settings(vm_id)
        if method == "vms.disk.resize":
            return await self.desktop.resize_disk(vm_id, payload)
        if method == "hardware.capabilities":
            return await self.desktop.probe.get(
                self.service.config.profile(payload.get("profile_id"))
            )
        if method == "vms.start":
            result = await self.service.start(vm_id)
            return (
                {
                    key: value
                    for key, value in result.model_dump(mode="json", exclude_unset=True).items()
                    if key != "ok"
                }
                if self.service.lifecycle.is_persistent(vm_id)
                else result.model_dump(mode="json")
            )
        if method == "vms.stop":
            return await self.desktop.stop(
                vm_id,
                force=payload.get("force", False),
                expected_instance_id=payload.get("expected_instance_id"),
            )
        if method == "vms.restart":
            return await self.desktop.restart(vm_id)
        if method == "vms.reset":
            result = await self.service.reset(vm_id)
            return (
                {
                    key: value
                    for key, value in result.model_dump(mode="json", exclude_unset=True).items()
                    if key != "ok"
                }
                if self.service.lifecycle.is_persistent(vm_id)
                else result.model_dump(mode="json")
            )
        if method == "vms.delete":
            return await self.desktop.delete(vm_id)
        if method == "display.ticket":
            return self.display.ticket(vm_id, str(payload["instance_id"]))
        if method == "display.sync":
            await self.service.observe(vm_id, actor="operator")
            return (await self.service.status(vm_id, actor="operator")).model_dump(mode="json")
        if method == "control.acquire":
            return await self.service.control.acquire(vm_id, str(payload["instance_id"]))
        if method == "control.heartbeat":
            return self.service.control.heartbeat(vm_id, str(payload["lease_id"]))
        if method == "control.release":
            return await self.service.control.release(vm_id, str(payload["lease_id"]))
        if method == "control.input":
            return await self.service.control.input(
                vm_id,
                str(payload["lease_id"]),
                payload["seq"],
                payload["display_epoch"],
                payload["width"],
                payload["height"],
                payload["events"],
            )
        if method == "runtime.stop":
            await self.service.manager.prepare_runtime_stop()
            asyncio.get_running_loop().call_later(0.1, self.stopping.set)
            return {"stopping": True}
        if method == "daemon.stop":
            if self.kind != "daemon":
                raise PaletteError(ErrorCode.STATE_CONFLICT, "this owner belongs to an MCP session")
            asyncio.get_running_loop().call_later(0.1, self.stopping.set)
            return {"stopping": True}
        raise PaletteError(ErrorCode.INVALID_ARGUMENT, "unknown desktop operation")
