"""One runtime owner, shared by a daemon or a standalone MCP session."""

from __future__ import annotations

import asyncio
import logging
import os
import time
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
    from .vm.manager import VmInstance


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
                    "vm-drives-v1",
                    "os-presets-v1",
                    "disk-resize",
                    "runtime-stop",
                    "vm-stop-identity",
                    "vm-reboot-v1",
                    "vm-shutdown-v2",
                    "driver-claim-v1",
                    "guest-enhancements-v1",
                    "operator-input-stream-v1",
                    "experimental-display-v1",
                    "display-settings-v1",
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
        if method == "vms.disk.delete":
            from .managed_disk_delete import delete_managed_disk

            return await delete_managed_disk(self.desktop, vm_id, payload)
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
        if method == "vms.reboot":
            expected = payload.get("expected_instance_id")
            if not isinstance(expected, str) or not expected:
                raise PaletteError(ErrorCode.INVALID_ARGUMENT, "expected_instance_id is required")
            return await self.desktop.reboot(vm_id, expected_instance_id=expected)
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
        if method == "display.settings.get":
            return self._display_settings()
        if method == "display.settings.update":
            self.service.manager.display_settings.update(payload)
            return self._display_settings()
        if method == "display.ticket":
            instance = self.service.manager.get(vm_id)
            self.service.require_instance_id(instance, str(payload["instance_id"]))
            if instance.state.value == "RUNNING":
                await self.service.manager.start_display_bridge(instance)
                if (
                    instance.display_bridge is not None
                    and not instance.display_bridge.source.closed
                ):
                    await instance.display_bridge.source.wait_frame()
            return self.display.ticket(vm_id, str(payload["instance_id"]))
        if method == "display.sync":
            try:
                await self.service.observe(vm_id, actor="operator")
            except PaletteError as error:
                if error.code != ErrorCode.VM_NOT_RUNNING:
                    raise
            return (await self.service.status(vm_id, actor="operator")).model_dump(mode="json")
        if method == "control.acquire":
            return await self.service.control.acquire(vm_id, str(payload["instance_id"]))
        if method == "display.shadow":
            instance = self.service.manager.get(vm_id)
            self.service.require_instance_id(instance, str(payload["instance_id"]))
            bridge = instance.display_bridge
            if bridge is None or bridge.source.closed or bridge.source.frame is None:
                raise PaletteError(ErrorCode.UNSUPPORTED_CAPABILITY, "raw display is unavailable")
            frame = bridge.source.frame
            captured = await bridge.source.shadow_capture(instance.profile.capture)
            return {
                "authoritative": False,
                "generation": frame.generation,
                "source_seq": frame.seq,
                "source_age_ms": (time.monotonic() - frame.received_at) * 1000,
                "duration_ms": captured.duration_ms,
                "byte_count": captured.byte_count,
                "native_size": captured.native_size.model_dump(),
                "image_size": captured.image_size.model_dump(),
            }
        if method == "control.metrics":
            from .capture.base import SplitCaptureBackend
            from .timing import process_tree_metrics

            instance = self.service.manager.get(vm_id)
            controller = instance.controller
            lease = self.service.control.leases.get(vm_id)
            return {
                "instance_id": instance.view.instance_id,
                "input_transport": (
                    "operator-input-stream-v1"
                    if lease is not None and (vm_id, lease.token) in self.operator._input_streams
                    else "one-shot"
                    if lease is not None
                    else "inactive"
                ),
                "input": controller.operator_timings.snapshot() if controller else {},
                "observation": controller.observation_timings.snapshot() if controller else {},
                "observation_pipeline": "split"
                if isinstance(instance.capture, SplitCaptureBackend)
                else "serial",
                "runtime": self.service.control.timings.snapshot(),
                "display": self.display.timings.snapshot(),
                "display_settings": self._display_instance_settings(instance),
                "resources": process_tree_metrics(),
                "execution": {
                    "qemu_version": instance.qmp.version if instance.qmp else None,
                    "guest_arch": instance.profile.qemu.arch,
                    "accelerator": instance.plan.accelerator if instance.plan else None,
                    "vcpus": instance.profile.resources.vcpus,
                    "memory_mib": instance.profile.resources.memory_mib,
                    "machine": instance.profile.qemu.machine,
                    "display_model": instance.profile.devices.display_model,
                    "capture_max_long_edge": instance.profile.capture.max_long_edge,
                    "capture_max_bytes": instance.profile.capture.max_bytes,
                },
            }
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

    def _display_settings(self) -> dict[str, Any]:
        manager = self.service.manager
        return {
            "configured_mode": manager.display_settings.mode,
            "reason": manager.display_settings.reason,
            "instances": [
                self._display_instance_settings(manager.get(vm_id))
                for vm_id in manager.known()
                if manager.get(vm_id).state.value in {"STARTING", "RUNNING", "PAUSED"}
            ],
        }

    def _display_instance_settings(self, instance: VmInstance) -> dict[str, Any]:
        bridge = instance.display_bridge
        provider = (
            "raw"
            if bridge is not None and not bridge.source.closed and bridge.source.frame is not None
            else "rfb"
            if instance.console_address is not None
            else None
        )
        mode = self.service.manager.display_settings.mode
        return {
            "vm_id": instance.view.vm_id,
            "instance_id": instance.view.instance_id,
            "provider": provider,
            "refresh_mode": "steady"
            if instance.plan and instance.plan.steady_display
            else "adaptive",
            "requested_mode": instance.display_requested_mode,
            "restart_required": mode != instance.display_requested_mode,
            "reason": instance.display_reason or self.service.manager.display_settings.reason,
        }
