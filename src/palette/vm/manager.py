"""VM lifecycle: states, startup sequence, stopping and clean reset.

The state machine implements the plan's diagram, and — just as importantly — keeps three
readiness questions apart, because collapsing them is how a service ends up telling an
agent "the desktop is ready" while QEMU is still showing firmware:

* ``qmp_ready``     — the management channel completed its handshake;
* ``screen_ready``  — a capture produced a valid framebuffer (which may still be firmware
  or a login prompt);
* ``desktop_ready`` — *not inferred*. It stays ``None`` unless the template or an explicit
  verification says otherwise.

A clean reset is "throw the writable layer away and make a new one", never ``system_reset``
and never ``qemu-img commit``: the base image is read-only for the lifetime of the service.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import time
import uuid
from collections import deque
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from ..audit import AuditLog
from ..capture.base import CaptureBackend
from ..capture.qmp import QmpCaptureBackend
from ..cleanup import join_cleanup
from ..config import PaletteConfig, Profile
from ..errors import ErrorCode, PaletteError
from ..guest_tools.base import DisabledGuestToolBackend, GuestToolBackend
from ..guest_tools.bridge import BridgeGuestToolBackend, GuestToolTransport
from ..input.controller import ActionController
from ..input.state import InstanceView
from ..models import ErrorPayload
from ..operations import VmOperations
from ..platforms import execution_info, validate_target
from ..policy import PathGuard, Policy, validate_identifier
from ..qmp.client import QmpClient
from ..qmp.transport import ProcessQmpTransport
from .images import ImageManager, InstancePaths
from .launcher import (
    LaunchOverrides,
    LaunchPlan,
    build_launch_plan,
    locate_binary,
    probe_binary_async,
    require_accelerator,
    resolve_firmware,
    spawn_qemu,
)
from .managed import ManagedProcess
from .process_guard import ProcessGuard, create_process_guard
from .storage import VmStorage

logger = logging.getLogger("palette.vm")


class ShutdownMode(StrEnum):
    GRACEFUL = "graceful"
    WITH_FALLBACK = "with_fallback"
    FORCE = "force"


class VmState(StrEnum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPING = "STOPPING"
    FAILED = "FAILED"


#: Legal transitions. Anything else is a bug or a race that must be reported, not smoothed
#: over.
_ALLOWED_TRANSITIONS: dict[VmState, frozenset[VmState]] = {
    VmState.STOPPED: frozenset({VmState.STARTING}),
    VmState.STARTING: frozenset({VmState.RUNNING, VmState.FAILED, VmState.STOPPING}),
    VmState.RUNNING: frozenset({VmState.PAUSED, VmState.STOPPING, VmState.FAILED}),
    VmState.PAUSED: frozenset({VmState.RUNNING, VmState.STOPPING, VmState.FAILED}),
    VmState.STOPPING: frozenset({VmState.STOPPED, VmState.FAILED}),
    VmState.FAILED: frozenset({VmState.STOPPED, VmState.STARTING}),
}


@dataclass
class VmInstance:
    """One running (or failed) QEMU process and everything attached to it."""

    view: InstanceView
    profile: Profile
    policy: Policy
    paths: InstancePaths
    cleanup_root: Path
    plan: LaunchPlan | None = None
    process: ManagedProcess | None = None
    transport: ProcessQmpTransport | None = None
    qmp: QmpClient | None = None
    capture: CaptureBackend | None = None
    controller: ActionController | None = None
    guest_tools: GuestToolBackend | None = None
    guard: ProcessGuard | None = None
    stderr_lines: deque[str] = field(default_factory=lambda: deque(maxlen=200))
    tasks: list[asyncio.Task[None]] = field(default_factory=list)
    started_monotonic: float = field(default_factory=time.monotonic)
    stop_requested: bool = False
    guest_shutdown: bool = False
    #: Host material the caller supplied for this instance. Kept so a reset rebuilds the
    #: instance that was asked for instead of quietly reverting to the profile's defaults.
    overrides: LaunchOverrides = field(default_factory=LaunchOverrides)
    guest_refresh: asyncio.Task[None] | None = None
    persistent_storage: bool = False
    console_address: tuple[str, int] | None = None
    console_password: str | None = None

    @property
    def state(self) -> VmState:
        return VmState(self.view.state)

    def transition(self, target: VmState) -> None:
        current = self.state
        if target == current:
            return
        if target not in _ALLOWED_TRANSITIONS[current]:
            raise PaletteError(
                ErrorCode.STATE_CONFLICT,
                f"illegal VM transition {current} -> {target}",
                details={"from": str(current), "to": str(target)},
            )
        self.view.state = str(target)

    def stderr_tail(self, limit: int = 12) -> list[str]:
        return list(self.stderr_lines)[-limit:]


class VmManager:
    """Owns the lifecycle of every served VM.

    One manager per service process, with serialized lifecycle changes and an independent
    action queue for each VM. Cross-VM resource scheduling is outside its scope.
    """

    def __init__(
        self,
        config: PaletteConfig,
        *,
        audit: AuditLog | None = None,
        guest_tool_transport_factory: object | None = None,
    ) -> None:
        from ..vm_settings import CapabilityProbe

        self.config = config
        self.probe = CapabilityProbe(config.host)
        self.audit = audit
        self._instances: dict[str, VmInstance] = {}
        self._boot_epochs: dict[str, int] = {}
        self.operations = VmOperations()
        self._runtime_stopping = False
        self._cleanup_waiters: set[asyncio.Task[Any]] = set()
        self._guest_tool_transport_factory = guest_tool_transport_factory
        self.storage = VmStorage(config)
        self.persistent_roots = self.storage.persistent_roots
        self.console_enabled = False

    # -- lookup ----------------------------------------------------------------------
    def get(self, vm_id: str) -> VmInstance:
        validate_identifier(vm_id, what="vm_id")
        instance = self._instances.get(vm_id)
        if instance is None:
            raise PaletteError(
                ErrorCode.VM_NOT_FOUND,
                f"no VM named {vm_id!r} is known to this service",
                details={"known_vms": sorted(self._instances)},
            )
        return instance

    def find(self, vm_id: str) -> VmInstance | None:
        return self._instances.get(vm_id)

    def forget(self, vm_id: str) -> None:
        instance = self.find(vm_id)
        if instance is not None and (
            instance.state not in {VmState.STOPPED, VmState.FAILED}
            or (instance.process is not None and instance.process.returncode is None)
        ):
            raise PaletteError(ErrorCode.STATE_CONFLICT, "the VM still owns runtime resources")
        self._instances.pop(vm_id, None)

    def known(self) -> list[str]:
        return sorted(self._instances)

    async def require_running(self, vm_id: str) -> VmInstance:
        instance = self.get(vm_id)
        if instance.state is not VmState.RUNNING:
            raise PaletteError(
                ErrorCode.VM_NOT_RUNNING,
                f"VM {vm_id!r} is {instance.state}",
                details={"state": str(instance.state)},
            )
        return instance

    # -- start -----------------------------------------------------------------------
    async def start(
        self,
        vm_id: str,
        profile_id: str | None = None,
        *,
        overrides: LaunchOverrides | None = None,
        profile_override: Profile | None = None,
        storage: InstancePaths | None = None,
        on_storage_ready: Callable[[VmInstance], None] | None = None,
    ) -> VmInstance:
        validate_identifier(vm_id, what="vm_id")
        profile = profile_override or self.config.profile(profile_id)
        with self.storage.protect(profile, overrides or LaunchOverrides(), storage):
            async with self.operations.hold(vm_id, starting=True):
                self._require_accepting_starts()
                existing = self._instances.get(vm_id)
                if existing is not None and existing.state in {
                    VmState.RUNNING,
                    VmState.STARTING,
                    VmState.PAUSED,
                }:
                    raise PaletteError(
                        ErrorCode.VM_ALREADY_RUNNING, f"VM {vm_id!r} is already running"
                    )
                try:
                    if existing is not None:
                        await self._finish_cleanup(self._stop_locked(existing, force=True))
                    return await self._start_locked(
                        vm_id, profile, overrides or LaunchOverrides(), storage, on_storage_ready
                    )
                finally:
                    self._apply_retention(vm_id, profile)

    async def _start_locked(
        self,
        vm_id: str,
        profile: Profile,
        overrides: LaunchOverrides,
        storage: InstancePaths | None = None,
        on_storage_ready: Callable[[VmInstance], None] | None = None,
    ) -> VmInstance:
        with self.storage.protect(profile, overrides, storage):
            self._require_accepting_starts()
            validate_target(profile)
            guard_root = PathGuard(self.config.runtime_root)
            policy = Policy(profile, guard_root)
            policy.require_network_supported()

            binary = locate_binary(profile, self.config.host)
            if binary is None:
                raise PaletteError(
                    ErrorCode.QEMU_NOT_FOUND,
                    "no QEMU system emulator was found; install it or set qemu.binary in the profile",
                    details={"profile": profile.profile_id},
                )
            probe = await probe_binary_async(binary, timeout=20.0, prefix=profile.qemu.argv_prefix)
            if not probe.found:
                raise PaletteError(
                    ErrorCode.QEMU_NOT_FOUND,
                    "the QEMU executable could not be identified",
                    details={"error": probe.error},
                )
            if (
                profile.qemu.version_pin
                and probe.version
                and not probe.version.startswith(profile.qemu.version_pin)
            ):
                raise PaletteError(
                    ErrorCode.PRECONDITION_FAILED,
                    "the installed QEMU version does not match the profile's pinned version",
                    details={"installed": probe.version, "pinned": profile.qemu.version_pin},
                )
            accelerator = require_accelerator(profile, probe)
            profile = resolve_firmware(profile, binary)
            if profile.audio.enabled:
                capabilities = await self.probe.get(profile)
                if not capabilities["audio_available"]:
                    raise PaletteError(
                        ErrorCode.UNSUPPORTED_CAPABILITY, "当前 QEMU 缺少输出声卡或宿主音频后端"
                    )
                profile = profile.model_copy(
                    update={
                        "audio": profile.audio.model_copy(
                            update={"backend": capabilities["audio_backend"]}
                        )
                    }
                )

            images = ImageManager(
                guard_root,
                profile,
                max_overlay_bytes=profile.resources.max_overlay_bytes,
                max_overlays=profile.retention.max_overlays,
                keep_overlays=profile.retention.keep_overlays,
            )
            self._check_free_space(profile)

            instance_id = f"inst_{uuid.uuid4().hex[:12]}"
            boot_epoch = self._boot_epochs.get(vm_id, 0) + 1
            self._boot_epochs[vm_id] = boot_epoch
            if storage is not None:
                guard_root.assert_managed(storage.overlay)
                if images.read_manifest(vm_id, storage.instance_id) is None:
                    raise PaletteError(
                        ErrorCode.IMAGE_INVALID, "persistent storage manifest is invalid"
                    )
                await images.inspect_image(storage.overlay, image_format="qcow2")
            paths = images.allocate_instance(vm_id, instance_id)
            if storage is not None:
                paths.overlay = storage.overlay
                paths.hardware_vars = storage.hardware_vars

            view = InstanceView(
                vm_id=vm_id,
                instance_id=instance_id,
                profile_id=profile.profile_id,
                display_id=profile.devices.display_id,
                head=profile.devices.head,
                boot_epoch=boot_epoch,
                state=str(VmState.STARTING),
                started_at=datetime.now().astimezone(),
            )
            instance = VmInstance(
                view=view,
                profile=profile,
                policy=policy,
                paths=paths,
                cleanup_root=paths.root,
                overrides=overrides,
                persistent_storage=storage is not None,
            )
            self._instances[vm_id] = instance

            try:
                async with asyncio.timeout(profile.timeouts.startup_seconds):
                    await self._provision(
                        instance,
                        images,
                        profile,
                        binary,
                        accelerator,
                        overrides,
                        storage,
                        on_storage_ready,
                    )
            except asyncio.CancelledError:
                await self._finish_cleanup(self._rollback_start(instance, images))
                raise
            except TimeoutError:
                error = PaletteError(
                    ErrorCode.TIMEOUT, "VM provisioning exceeded its startup budget"
                )
                view.last_error = ErrorPayload(**error.as_payload())
                await self._finish_cleanup(self._rollback_start(instance, images))
                raise error from None
            except PaletteError as exc:
                view.last_error = ErrorPayload(**exc.as_payload())
                await self._finish_cleanup(self._rollback_start(instance, images))
                raise
            except Exception as exc:
                logger.exception("unexpected failure starting %s", vm_id)
                error = PaletteError(
                    ErrorCode.STARTUP_FAILED, f"startup failed: {type(exc).__name__}"
                )
                view.last_error = ErrorPayload(**error.as_payload())
                await self._finish_cleanup(self._rollback_start(instance, images))
                raise error from exc

            if self.audit:
                self.audit.record(
                    "vm.started",
                    vm_id=vm_id,
                    instance_id=instance_id,
                    profile_id=profile.profile_id,
                    accelerator=accelerator,
                    boot_epoch=boot_epoch,
                )
            return instance

    async def _finish_cleanup(self, operation: Coroutine[Any, Any, None]) -> None:
        """Finish owned-resource cleanup even if the requesting task is cancelled again."""
        waiter = asyncio.current_task()
        if waiter is not None:
            self._cleanup_waiters.add(waiter)
        task = asyncio.create_task(operation, name="vm-cleanup")
        try:
            await join_cleanup(task)
        finally:
            if waiter is not None:
                self._cleanup_waiters.discard(waiter)

    async def _rollback_start(self, instance: VmInstance, images: ImageManager) -> None:
        instance.stop_requested = True
        instance.view.state = str(VmState.FAILED)
        await self._teardown(instance, force=True)
        images.update_manifest(instance.paths, {"state": "FAILED", "process_exited": True})
        if not instance.persistent_storage:
            with contextlib.suppress(PaletteError, OSError):
                images.delete_instance(instance.view.vm_id, instance.view.instance_id)
        if self.find(instance.view.vm_id) is instance:
            self._instances.pop(instance.view.vm_id)

    async def _provision(
        self,
        instance: VmInstance,
        images: ImageManager,
        profile: Profile,
        binary: str,
        accelerator: str,
        overrides: LaunchOverrides,
        storage: InstancePaths | None = None,
        on_storage_ready: Callable[[VmInstance], None] | None = None,
    ) -> None:
        """The startup sequence, in the order the plan requires."""
        paths = instance.paths

        # 0. register the instance before anything can fail, so the failure path below can
        #    clean up through the same manifest-verified route as any other deletion.
        images.update_manifest(
            paths,
            {
                **execution_info(profile),
                "base_image": str(overrides.image) if overrides.image else profile.images.base,
                "base_from_caller": overrides.image is not None,
                "overlay": str(paths.overlay),
                "attached_volumes": [str(volume.path) for volume in overrides.volumes],
                "state": str(VmState.STARTING),
                "host_boot_epoch": instance.view.boot_epoch,
                "firmware_code": profile.qemu.firmware.code,
                "firmware_vars": profile.qemu.firmware.vars,
            },
        )

        # 1. writable layer from the read-only base, with the backing chain verified. A
        #    caller-supplied image replaces the profile's pinned base here and nowhere else:
        #    the writable layer is still ours, per-instance, and discarded by reset.
        if storage is None:
            await images.create_overlay(
                paths,
                base=overrides.image,
                base_format=overrides.image_format,
                size_gib=profile.images.size_gib if on_storage_ready is not None else None,
            )
            images.stage_firmware_vars(paths)
        if on_storage_ready is not None:
            on_storage_ready(instance)
            instance.persistent_storage = True
        volumes = []
        volume_chain: list[str] = []
        for volume in overrides.volumes:
            info = await images.inspect_image(
                Path(volume.path), image_format="raw" if volume.media == "cdrom" else None
            )
            volumes.append(replace(volume, format=info.format))
            volume_chain.extend(str(path) for path in info.chain)
        overrides = replace(overrides, volumes=tuple(volumes))
        instance.overrides = overrides
        images.update_manifest(
            paths,
            {
                "volume_backing_chain": volume_chain,
                "volume_formats": [volume.format for volume in volumes],
            },
        )

        # 2. command line, then the process itself, hidden and with a whitelisted env.
        plan = build_launch_plan(
            profile=profile,
            binary=binary,
            instance_id=paths.instance_id,
            vm_id=paths.vm_id,
            overlay_path=paths.overlay,
            workdir=paths.root,
            hardware_vars_path=paths.hardware_vars,
            accelerator=accelerator,
            start_paused=True,
            extra_volumes=overrides.volumes,
            caller_args=overrides.qemu_args,
            managed_console=self.console_enabled,
        )
        instance.plan = plan
        process = await spawn_qemu(
            plan, on_spawn=lambda process: setattr(instance, "process", process)
        )
        instance.process = process

        # 3. orphan protection before anything else can fail.
        guard = (
            process
            if isinstance(process, ProcessGuard)
            else create_process_guard(
                memory_limit_bytes=(
                    profile.resources.process_memory_limit_mib * 1024 * 1024
                    if profile.resources.process_memory_limit_mib
                    else None
                )
            )
        )
        instance.guard = guard
        if not guard.attach(process.pid):
            raise PaletteError(
                ErrorCode.STARTUP_FAILED,
                "the required QEMU process guard could not be activated",
                details={"guard": guard.description},
            )

        # 4. drain stderr continuously: an unread pipe eventually blocks the guest.
        instance.tasks.append(
            asyncio.create_task(self._consume_stderr(instance), name=f"stderr-{paths.vm_id}")
        )

        # 5. QMP handshake over the private pipes (never stdio of this process).
        transport = ProcessQmpTransport(process, name=f"qmp-{paths.vm_id}")
        instance.transport = transport
        qmp = QmpClient(
            transport,
            command_timeout=profile.timeouts.qmp_command_seconds,
            handshake_timeout=profile.timeouts.qmp_handshake_seconds,
        )
        instance.qmp = qmp
        qmp.on_disconnect(lambda failure: self._on_qmp_disconnect(instance, failure))
        qmp.on_event(lambda name, data: self._on_qmp_event(instance, name, data))
        try:
            await qmp.start()
            await qmp.probe()
        except PaletteError as exc:
            raise PaletteError(
                ErrorCode.STARTUP_FAILED,
                f"QMP negotiation failed: {exc.code}",
                details={"detail": exc.message, "stderr": instance.stderr_tail(6)},
            ) from exc

        instance.view.qmp_ready = True
        if self.console_enabled and {"query-vnc", "set_password"}.issubset(qmp.commands or ()):
            import secrets

            password = secrets.token_hex(4)
            await qmp.execute("set_password", {"protocol": "vnc", "password": password})
            display = await qmp.execute("query-vnc")
            if display.get("enabled") and display.get("service"):
                instance.console_address = ("127.0.0.1", int(display["service"]))
                instance.console_password = password
        await qmp.execute("cont")
        instance.transition(VmState.RUNNING)

        capture = QmpCaptureBackend(qmp, profile.capture)
        instance.capture = capture
        controller = ActionController(
            view=instance.view,
            profile=profile,
            qmp=qmp,
            capture=capture,
            frames_dir=paths.frames,
            audit=self.audit,
        )
        instance.controller = controller
        await controller.start()

        # 6. guest enhancement channel only for a hybrid profile, and never required by
        #    the visual baseline.
        instance.guest_tools = DisabledGuestToolBackend()
        self._refresh_guest_tools(instance)

        instance.tasks.append(
            asyncio.create_task(self._watch_process(instance), name=f"watch-{paths.vm_id}")
        )
        if profile.resources.process_max_runtime_seconds:
            instance.tasks.append(
                asyncio.create_task(self._enforce_runtime(instance), name=f"runtime-{paths.vm_id}")
            )

        # 7. bounded readiness polling: report the levels reached, never invent a desktop.
        await self._poll_readiness(instance)
        images.update_manifest(
            paths,
            {
                "overlay": str(paths.overlay),
                "state": str(VmState.RUNNING),
                "host_boot_epoch": instance.view.boot_epoch,
            },
        )

    async def _build_guest_tools(self, instance: VmInstance) -> GuestToolBackend:
        config = instance.profile.guest_tools
        if not config.enabled:
            return DisabledGuestToolBackend()
        factory = self._guest_tool_transport_factory
        if factory is None:
            from ..guest_tools.bridge import VirtioSerialGuestToolTransport

            transport: GuestToolTransport = VirtioSerialGuestToolTransport()
        elif callable(factory):
            transport = factory(instance)
        else:  # pragma: no cover - defensive
            transport = factory  # type: ignore[assignment]
        bridge = BridgeGuestToolBackend(
            transport=transport,
            config=config,
            vm_id=instance.view.vm_id,
            instance_id=instance.view.instance_id,
            boot_epoch=instance.view.boot_epoch,
        )
        try:
            capabilities = await bridge.connect()
        except BaseException:
            await bridge.close()
            raise
        if not capabilities.usable:
            logger.info(
                "guest enhancement channel unavailable for %s: %s",
                instance.view.vm_id,
                capabilities.detail,
            )
        return bridge

    def _refresh_guest_tools(self, instance: VmInstance) -> None:
        previous = instance.guest_tools
        instance.guest_tools = DisabledGuestToolBackend()
        if instance.guest_refresh is not None:
            instance.guest_refresh.cancel()
        task = asyncio.create_task(
            self._reconnect_guest_tools(instance, previous, instance.view.boot_epoch),
            name=f"guest-connect-{instance.view.vm_id}",
        )
        instance.guest_refresh = task
        instance.tasks.append(task)

    async def _reconnect_guest_tools(
        self, instance: VmInstance, previous: GuestToolBackend | None, boot_epoch: int
    ) -> None:
        backend: GuestToolBackend | None = None
        try:
            if previous is not None:
                await previous.close()
            backend = await self._build_guest_tools(instance)
            if (
                self.find(instance.view.vm_id) is instance
                and not instance.stop_requested
                and instance.view.boot_epoch == boot_epoch
            ):
                instance.guest_tools = backend
                backend = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "guest reconnect unavailable for %s: %s", instance.view.vm_id, type(exc).__name__
            )
        finally:
            if backend is not None:
                await backend.close()

    async def _poll_readiness(self, instance: VmInstance) -> None:
        interval = instance.profile.timeouts.boot_poll_seconds
        controller = instance.controller
        if controller is None:  # pragma: no cover - guaranteed by _provision
            raise PaletteError(ErrorCode.INTERNAL_ERROR, "this VM has no active session")
        while True:
            try:
                await controller.observe()
            except PaletteError as exc:
                logger.debug("readiness capture not available yet: %s", exc.code)
            else:
                # A valid framebuffer is progress, not proof of a desktop: firmware and
                # the login screen both satisfy it.
                instance.view.screen_ready = True
                return
            if instance.process is not None and instance.process.returncode is not None:
                raise PaletteError(
                    ErrorCode.STARTUP_FAILED,
                    "QEMU exited during startup",
                    details={"returncode": instance.process.returncode},
                )
            await asyncio.sleep(interval)

    # -- stop ------------------------------------------------------------------------
    @staticmethod
    def check_identity(
        instance: VmInstance | None, expected_instance_id: str | None, expect_absent: bool = False
    ) -> None:
        if (expect_absent and instance is not None) or (
            expected_instance_id is not None
            and (instance is None or instance.view.instance_id != expected_instance_id)
        ):
            raise PaletteError(
                ErrorCode.STATE_CONFLICT, "the VM changed during lifecycle authorization"
            )

    async def shutdown_instance(
        self, vm_id: str, *, mode: ShutdownMode, expected_instance_id: str | None = None
    ) -> None:
        if mode is ShutdownMode.GRACEFUL:
            await self.graceful_stop(vm_id, expected_instance_id=expected_instance_id)
        else:
            await self.stop(
                vm_id, force=mode is ShutdownMode.FORCE, expected_instance_id=expected_instance_id
            )

    async def stop(
        self,
        vm_id: str,
        *,
        force: bool = False,
        keep_instance: bool = True,
        expected_instance_id: str | None = None,
    ) -> VmInstance:
        async with self.operations.hold(vm_id):
            instance = self.get(vm_id)
            self.check_identity(instance, expected_instance_id)
            try:
                await self._finish_cleanup(self._stop_locked(instance, force=force))
            finally:
                self._apply_retention(vm_id, instance.profile)
            if not keep_instance:
                self._instances.pop(vm_id, None)
            return instance

    async def _stop_locked(self, instance: VmInstance, *, force: bool) -> None:
        if instance.state in {VmState.STOPPED}:
            return
        instance.stop_requested = True
        instance.view.state = str(VmState.STOPPING)

        # Revoke control first so nothing new can be submitted while we unwind.
        release = None
        if instance.controller is not None:
            with contextlib.suppress(Exception):
                release = await instance.controller.shutdown(release=True)
        if release is not None and not release.release_confirmed:
            logger.warning(
                "could not confirm that all inputs were released on %s: %s",
                instance.view.vm_id,
                release.note,
            )

        process = instance.process
        if process is not None and process.returncode is None:
            if not force and instance.qmp is not None and instance.qmp.connected:
                with contextlib.suppress(PaletteError):
                    await instance.qmp.execute("system_powerdown")
                try:
                    await self._await_exit(
                        instance, timeout=instance.profile.timeouts.shutdown_seconds
                    )
                except PaletteError as exc:
                    # system_powerdown is a request, never a guarantee. Falling through to
                    # the forced path is the documented behaviour; failing the stop here
                    # would leave a process the operator asked us to remove.
                    logger.info(
                        "graceful shutdown did not complete for %s: %s",
                        instance.view.vm_id,
                        exc.code,
                    )
            if process.returncode is None:
                if force:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                await self._await_exit(
                    instance, timeout=instance.profile.timeouts.force_kill_seconds, kill=True
                )

        try:
            await self._teardown(instance)
        except BaseException:
            instance.view.state = str(VmState.FAILED)
            raise
        instance.transition(VmState.STOPPED)
        images = self.images_for(instance.profile)
        if instance.paths.manifest.exists():
            images.update_manifest(instance.paths, {"state": "STOPPED", "process_exited": True})
        if self.audit:
            self.audit.record(
                "vm.stopped",
                vm_id=instance.view.vm_id,
                instance_id=instance.view.instance_id,
                forced=force,
            )

    async def _await_exit(
        self, instance: VmInstance, *, timeout: float, kill: bool = False
    ) -> None:
        process = instance.process
        if process is None:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
            return
        except TimeoutError:
            pass
        if not kill:
            raise PaletteError(
                ErrorCode.TIMEOUT,
                "the guest did not shut down within the configured budget",
                details={
                    "operation": "guest-shutdown",
                    "vm_id": instance.view.vm_id,
                    "instance_id": instance.view.instance_id,
                    "timeout_seconds": timeout,
                },
            )
        logger.warning("force-killing QEMU for %s", instance.view.vm_id)
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except TimeoutError:
            instance.view.state = str(VmState.FAILED)
            raise PaletteError(
                ErrorCode.TIMEOUT, "QEMU exit could not be confirmed after killing it"
            ) from None

    async def _teardown(self, instance: VmInstance, *, force: bool = False) -> None:
        """Release every host resource attached to one instance.

        Ordered from the outside in: stop consuming, close the channel, then the process,
        then the guard — so the guard is always the last thing holding the process.
        """
        tasks = [
            task
            for task in instance.tasks
            if task is not asyncio.current_task() and task not in self._cleanup_waiters
        ]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        instance.tasks.clear()

        if instance.controller is not None:
            with contextlib.suppress(Exception):
                await instance.controller.shutdown(release=False)
        if instance.guest_tools is not None:
            with contextlib.suppress(Exception):
                await instance.guest_tools.close()
        if instance.qmp is not None:
            with contextlib.suppress(Exception):
                await instance.qmp.close()
        process = instance.process
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except TimeoutError:
                instance.view.state = str(VmState.FAILED)
                raise PaletteError(
                    ErrorCode.TIMEOUT, "QEMU is still alive; its disk has been retained"
                ) from None
        if instance.guard is not None:
            instance.guard.close()
        instance.view.qmp_ready = False
        instance.view.screen_ready = False
        instance.view.desktop_ready = None
        if force:
            instance.view.state = str(VmState.FAILED)

    # -- reset -----------------------------------------------------------------------
    async def reset(
        self,
        vm_id: str,
        profile_id: str | None = None,
        *,
        expected_instance_id: str | None = None,
        expect_absent: bool = False,
    ) -> VmInstance:
        """Abandon current state and rebuild from the base image.

        Steps, in order: revoke control, cancel the running action and release inputs, stop
        the process, keep the old overlay under the retention policy (**never** commit it
        to the base), then start a fresh instance with a new identifier and boot epoch.
        """
        validate_identifier(vm_id, what="vm_id")
        async with self.operations.hold(vm_id, starting=True):
            self._require_accepting_starts()
            instance = self.find(vm_id)
            self.check_identity(instance, expected_instance_id, expect_absent)
            profile = instance.profile if instance is not None else self.config.profile(profile_id)
            overrides = instance.overrides if instance is not None else LaunchOverrides()
            try:
                if instance is not None:
                    await self._finish_cleanup(self._stop_locked(instance, force=False))
                if self.audit:
                    self.audit.record(
                        "vm.reset",
                        vm_id=vm_id,
                        previous_instance=instance.view.instance_id if instance else None,
                    )
                return await self._start_locked(vm_id, profile, overrides)
            finally:
                self._apply_retention(vm_id, profile)

    def images_for(self, profile: Profile) -> ImageManager:
        return self.storage.images_for(profile)

    def _apply_retention(self, vm_id: str, profile: Profile) -> None:
        self.storage.prune(vm_id, profile, list(self._instances.values()))

    async def take_control(self, vm_id: str, *, expected_instance_id: str | None = None) -> int:
        async with self.operations.hold(vm_id):
            instance = self.get(vm_id)
            self.check_identity(instance, expected_instance_id)
            instance.view.controller_epoch += 1
            if instance.controller is not None:
                instance.controller.invalidate(reason="control changed hands")
                await self._finish_cleanup(self._release_controller(instance))
            return instance.view.controller_epoch

    async def graceful_stop(self, vm_id: str, *, expected_instance_id: str | None = None) -> None:
        """Desktop shutdown: a failed ACPI request must never turn into a kill."""
        async with self.operations.hold(vm_id):
            instance = self.get(vm_id)
            self.check_identity(instance, expected_instance_id)
            if instance.process is None or instance.process.returncode is not None:
                await self._finish_cleanup(self._stop_locked(instance, force=False))
                return
            if instance.qmp is None or not instance.qmp.connected:
                raise PaletteError(ErrorCode.QMP_UNAVAILABLE, "graceful shutdown is unavailable")
            previous = instance.view.state
            previous_stop_requested = instance.stop_requested
            instance.stop_requested = True
            instance.view.state = str(VmState.STOPPING)
            try:
                await instance.qmp.execute("system_powerdown")
                await self._await_exit(instance, timeout=instance.profile.timeouts.shutdown_seconds)
            except BaseException:
                instance.view.state = previous
                instance.stop_requested = previous_stop_requested
                raise
            await self._finish_cleanup(self._stop_locked(instance, force=False))
            self._apply_retention(vm_id, instance.profile)

    @staticmethod
    async def _release_controller(instance: VmInstance) -> None:
        if instance.controller is not None:
            with contextlib.suppress(PaletteError):
                await instance.controller.emergency_release(invalidate=False)

    async def emergency_stop_all(self) -> list[str]:
        """Operator path: stop everything now, without waiting for a graceful guest."""
        stopped: list[str] = []
        for vm_id, instance in list(self._instances.items()):
            try:
                await self.stop(vm_id, force=True, expected_instance_id=instance.view.instance_id)
            except PaletteError as exc:
                logger.error("emergency stop of %s failed: %s", vm_id, exc.code)
            else:
                stopped.append(vm_id)
        return stopped

    def _require_accepting_starts(self) -> None:
        if self._runtime_stopping or self.operations.closing:
            raise PaletteError(ErrorCode.STATE_CONFLICT, "the runtime is stopping")

    async def prepare_runtime_stop(self) -> None:
        """Seal the start gate only when every QEMU process has actually exited."""
        if self.operations.starting or any(
            instance.process is not None and instance.process.returncode is None
            for instance in self._instances.values()
        ):
            raise PaletteError(
                ErrorCode.STATE_CONFLICT,
                "virtual machines are still running; shut them down before exiting",
            )
        self._runtime_stopping = True
        self.operations.closing = True

    async def shutdown(self) -> None:
        await self._finish_cleanup(self._shutdown_all())

    async def _shutdown_all(self) -> None:
        await self.operations.seal()
        await self.probe.close()
        failure: Exception | None = None
        for vm_id in list(self._instances):
            try:
                await self.stop(vm_id, force=False)
            except Exception as exc:
                failure = failure or exc
                logger.error("shutdown of %s failed: %s", vm_id, type(exc).__name__)
        if failure is not None:
            raise failure

    # -- background tasks ------------------------------------------------------------
    async def _consume_stderr(self, instance: VmInstance) -> None:
        process = instance.process
        if process is None or process.stderr is None:
            return
        try:
            while True:
                line = await process.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").rstrip()
                if text:
                    instance.stderr_lines.append(text)
                    logger.debug("qemu[%s]: %s", instance.view.vm_id, text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("stderr consumer stopped: %s", exc)

    async def _watch_process(self, instance: VmInstance) -> None:
        process = instance.process
        if process is None:
            return
        code = await process.wait()
        # Process exit and pipe delivery are independent on Windows. Drain QMP so
        # a final guest SHUTDOWN cannot arrive after we report a crash.
        if instance.qmp is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    instance.qmp.wait_closed(),
                    timeout=instance.profile.timeouts.qmp_command_seconds,
                )
        if instance.stop_requested:
            return
        if instance.guest_shutdown and code == 0:
            async with self.operations.hold(instance.view.vm_id):
                if self.find(instance.view.vm_id) is not instance or instance.stop_requested:
                    return
                await self._finish_cleanup(self._stop_locked(instance, force=False))
                self._apply_retention(instance.view.vm_id, instance.profile)
            return
        logger.warning("QEMU for %s exited unexpectedly with %s", instance.view.vm_id, code)
        instance.view.state = str(VmState.FAILED)
        error = PaletteError(
            ErrorCode.QMP_UNAVAILABLE,
            "the QEMU process exited; this instance, its frames and its control are void",
            details={"returncode": code},
        )
        instance.view.last_error = ErrorPayload(**error.as_payload())
        instance.view.qmp_ready = False
        instance.view.screen_ready = False
        if self.audit:
            self.audit.record(
                "vm.exited",
                vm_id=instance.view.vm_id,
                instance_id=instance.view.instance_id,
                code=code,
            )

    async def _enforce_runtime(self, instance: VmInstance) -> None:
        limit = instance.profile.resources.process_max_runtime_seconds
        if not limit:
            return
        await asyncio.sleep(limit)
        logger.warning("runtime limit reached for %s; stopping", instance.view.vm_id)
        with contextlib.suppress(PaletteError):
            await self.stop(
                instance.view.vm_id, force=True, expected_instance_id=instance.view.instance_id
            )

    def _on_qmp_event(self, instance: VmInstance, name: str, data: dict[str, Any]) -> None:
        if self.find(instance.view.vm_id) is not instance:
            return
        if (
            name == "SHUTDOWN"
            and data.get("guest") is True
            and data.get("reason") == "guest-shutdown"
        ):
            instance.guest_shutdown = True
            if not instance.stop_requested:
                instance.view.state = str(VmState.STOPPING)
                instance.view.qmp_ready = False
                instance.view.screen_ready = False
                instance.view.desktop_ready = None
                if instance.controller is not None:
                    instance.controller.invalidate(reason="the guest shut down")
            return
        if (
            name != "RESET"
            or instance.stop_requested
            or self.find(instance.view.vm_id) is not instance
        ):
            return
        epoch = self._boot_epochs.get(instance.view.vm_id, instance.view.boot_epoch) + 1
        self._boot_epochs[instance.view.vm_id] = epoch
        instance.view.boot_epoch = epoch
        instance.view.screen_ready = False
        instance.view.desktop_ready = None
        if instance.controller is not None:
            instance.controller.invalidate(reason="the guest rebooted")
            instance.tasks.append(
                asyncio.create_task(
                    self._release_controller(instance), name=f"reset-release-{instance.view.vm_id}"
                )
            )
        self._refresh_guest_tools(instance)
        with contextlib.suppress(PaletteError, OSError):
            self.images_for(instance.profile).update_manifest(
                instance.paths, {"host_boot_epoch": epoch}
            )
        if self.audit:
            self.audit.record(
                "vm.guest_reset",
                vm_id=instance.view.vm_id,
                instance_id=instance.view.instance_id,
                boot_epoch=epoch,
            )

    def _on_qmp_disconnect(self, instance: VmInstance, failure: BaseException | None) -> None:
        if instance.stop_requested or instance.guest_shutdown:
            return
        logger.warning("QMP channel for %s dropped: %s", instance.view.vm_id, failure)
        instance.view.qmp_ready = False
        instance.view.screen_ready = False
        instance.view.desktop_ready = None
        if instance.controller is not None:
            instance.controller.invalidate(reason="the management channel disconnected")
        if instance.state in {VmState.RUNNING, VmState.PAUSED}:
            # The guest may still be alive, but the service can no longer observe or
            # control it, so it must not be reported as usable.
            instance.view.state = str(VmState.FAILED)

    # -- checks ----------------------------------------------------------------------
    def _check_free_space(self, profile: Profile) -> None:
        try:
            usage = shutil.disk_usage(self.config.runtime_root)
        except OSError:
            return
        required = profile.images.size_gib * 1024**3 // 4  # overlays grow lazily
        if usage.free < required:
            raise PaletteError(
                ErrorCode.PRECONDITION_FAILED,
                "not enough free space on the runtime volume for this profile",
                details={
                    "free_bytes": usage.free,
                    "required_bytes": required,
                },
            )
