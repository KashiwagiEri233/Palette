"""One runtime owner, shared by a daemon or a standalone MCP session."""

from __future__ import annotations

import asyncio
import os
from typing import Any

from . import __version__
from .desktop import DesktopManager
from .display import DisplayGateway
from .errors import ErrorCode, PaletteError
from .operator.protocol import DesktopCommand
from .operator.server import OperatorServer
from .service import PaletteService


class RuntimeOwner:
    def __init__(self, service: PaletteService, *, kind: str = "embedded") -> None:
        self.service = service
        self.kind = kind
        self.operator = OperatorServer(service)
        self.desktop = DesktopManager(service)
        self.display = DisplayGateway(service)
        self.stopping = asyncio.Event()
        self.operator.desktop_route = self.route
        self.operator.desktop_manager = self.desktop
        self.agent: Any = None

    async def start(self) -> None:
        from .agent_ipc import AgentServer

        self.operator.acquire()
        try:
            await self.service.startup()
            await self.display.start()
            self.service.manager.console_enabled = True
            self.agent = AgentServer(self.service, self.operator, self.desktop)
            await self.agent.start()
            await self.operator.start()
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        try:
            await self.display.close()
            await self.operator.stop()
            if self.agent is not None:
                await self.agent.stop()
            await self.service.shutdown()
        finally:
            self.operator.release()

    async def route(self, command: DesktopCommand) -> dict[str, Any]:
        method, payload = command.method, command.payload
        vm_id = command.vm_id or ""
        if method == "hello":
            return {
                "version": __version__,
                "desktop_protocol": 1,
                "owner_kind": self.kind,
                "pid": os.getpid(),
                "generation": self.operator.endpoint.generation,
                "runtime_root": str(self.service.config.runtime_root),
                "config_root": str(self.service.config.config_root),
                "agent_endpoint": self.agent.endpoint.model_dump(mode="json"),
            }
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
        if method == "doctor":
            from .cli import build_doctor_report

            return await asyncio.to_thread(
                build_doctor_report, self.service.config, profile_id=payload.get("profile_id")
            )
        if method == "image.info":
            from .vm.images import validate_caller_path

            profile = self.service.config.profile(payload.get("profile_id"))
            path = validate_caller_path(payload["path"], what="image")
            info = await self.service.manager._images(profile).inspect_image(path)
            return {"format": info.format, "virtual_size": info.virtual_size}
        if method == "vms.list":
            return {"vms": await self.desktop.list()}
        if method == "vms.create":
            return await self.desktop.create(payload)
        if method == "vms.update":
            return await self.desktop.update(vm_id, payload)
        if method == "vms.start":
            if vm_id in self.desktop.definitions:
                return await self.desktop.start(vm_id)
            return (await self.service.start(vm_id)).model_dump(mode="json")
        if method == "vms.stop":
            return await self.desktop.stop(vm_id, force=payload.get("force", False))
        if method == "vms.restart":
            return await self.desktop.restart(vm_id)
        if method == "vms.reset":
            if vm_id in self.desktop.definitions:
                return await self.desktop.reset(vm_id)
            return (await self.service.reset(vm_id)).model_dump(mode="json")
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
        if method == "daemon.stop":
            if self.kind != "daemon":
                raise PaletteError(ErrorCode.STATE_CONFLICT, "this owner belongs to an MCP session")
            asyncio.get_running_loop().call_later(0.1, self.stopping.set)
            return {"stopping": True}
        raise PaletteError(ErrorCode.INVALID_ARGUMENT, "unknown desktop operation")
