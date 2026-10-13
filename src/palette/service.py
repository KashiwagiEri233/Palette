"""Orchestration: policy, dispatch, audit and lifecycle in one place.

The service is the only component that knows about *both* the tool surface and the
machinery underneath, which is what makes it the right place to enforce the rules that
must hold no matter which tool was called:

* every request is authorized against the profile before anything happens;
* an ``instance_id`` from the agent is verified against the running instance, so a stale
  request cannot be applied to a replacement process that happens to reuse the name;
* guest enhancement calls share the same per-VM scheduler, action sequence and audit
  trail as visual clicks;
* a failure is reported with a stable code, never with a host path or the text that was
  typed.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal

from .audit import AuditLog, NullAuditLog
from .config import PaletteConfig
from .errors import ErrorCode, PaletteError
from .guest_tools.base import GuestToolOutcome
from .input.controller import ActionController
from .models import (
    DisplayInfo,
    ErrorPayload,
    Frame,
    GuestToolResult,
    GuestToolStatus,
    InputLedgerState,
    LifecycleResult,
    ObserveResult,
    Receipt,
    ReleaseResult,
    StatusResult,
    VmStatus,
    WaitResult,
)
from .policy import PathGuard, Policy, validate_identifier
from .vm.images import validate_caller_path
from .vm.launcher import ExtraVolume, LaunchOverrides
from .vm.manager import VmInstance, VmManager, VmState

logger = logging.getLogger("palette.service")

#: Roles a request can be attributed to. Only trusted call sites choose a role; an agent
#: cannot ask for one.
Actor = Literal["agent", "operator"]


class PaletteService:
    """The application object behind every MCP tool and every CLI command."""

    def __init__(
        self,
        config: PaletteConfig,
        *,
        audit: AuditLog | None = None,
        guest_tool_transport_factory: object | None = None,
    ) -> None:
        self.config = config
        self.audit = audit or NullAuditLog()
        self.manager = VmManager(
            config,
            audit=self.audit,
            guest_tool_transport_factory=guest_tool_transport_factory,
        )
        from .control import ControlManager

        self.control = ControlManager(self)

    # -- lifecycle --------------------------------------------------------------------
    async def startup(self) -> None:
        self.config.runtime_root.mkdir(parents=True, exist_ok=True)
        self.control.start()

    async def shutdown(self) -> None:
        await self.control.close()
        await self.manager.shutdown()

    # -- helpers ----------------------------------------------------------------------
    def policy_for(self, vm_id: str) -> Policy:
        return self.manager.get(vm_id).policy

    def _instance(self, vm_id: str) -> VmInstance:
        return self.manager.get(vm_id)

    def _require_instance_id(self, instance: VmInstance, instance_id: str) -> None:
        """Bind a request to the process it was written for.

        Without this a request that outlived a reset could be applied to the *new*
        instance, which is exactly the class of mistake the epoch fields exist to prevent.
        """
        validate_identifier(instance_id, what="instance_id")
        if instance.view.instance_id != instance_id:
            raise PaletteError(
                ErrorCode.STALE_FRAME,
                "this request names a different VM instance than the one running now",
                details={"expected_instance_id": instance.view.instance_id},
            )

    def _controller(self, instance: VmInstance) -> ActionController:
        if instance.controller is None:
            raise PaletteError(
                ErrorCode.VM_NOT_RUNNING,
                f"VM {instance.view.vm_id!r} has no active session",
                details={"state": str(instance.state)},
            )
        return instance.controller

    # -- observation ------------------------------------------------------------------
    async def status(self, vm_id: str, *, actor: Actor = "agent") -> StatusResult:
        from .platforms import execution_info

        instance = self.manager.find(vm_id)
        if instance is None:
            return StatusResult(
                ok=False,
                error=ErrorPayload(
                    **PaletteError(
                        ErrorCode.VM_NOT_FOUND,
                        f"no VM named {vm_id!r} is known to this service",
                        details={"known_vms": self.manager.known()},
                    ).as_payload()
                ),
                capabilities=[],
            )
        controller = instance.controller
        ledger: InputLedgerState
        if controller is not None:
            snapshot = controller.ledger_snapshot()
            ledger = InputLedgerState(keys=snapshot["keys"], buttons=snapshot["buttons"])
        else:
            ledger = InputLedgerState(release_confirmed=False)
        guest = instance.guest_tools
        guest_status = await guest.capabilities() if guest is not None else None
        status = VmStatus(
            **execution_info(instance.profile),
            vm_id=vm_id,
            profile_id=instance.profile.profile_id,
            instance_id=instance.view.instance_id,
            state=str(instance.state),
            boot_epoch=instance.view.boot_epoch,
            display_epoch=instance.view.display_epoch,
            controller_epoch=instance.view.controller_epoch,
            action_seq=instance.view.action_seq,
            qmp_ready=instance.view.qmp_ready and (controller is not None),
            screen_ready=instance.view.screen_ready,
            desktop_ready=instance.view.desktop_ready,
            display=(
                DisplayInfo(
                    display_id=instance.view.display_id,
                    head=instance.view.head,
                    width=instance.view.native_size.width,
                    height=instance.view.native_size.height,
                )
                if instance.view.native_size is not None
                else None
            ),
            input_ledger=ledger,
            guest_tools=(
                GuestToolStatus(
                    enabled=guest_status.enabled,
                    transport_online=guest_status.transport_online,
                    service_online=guest_status.service_online,
                    interactive_desktop_ready=guest_status.interactive_desktop_ready,
                    protocol_version=guest_status.protocol_version,
                    connection_generation=guest_status.connection_generation,
                    capabilities=list(guest_status.capabilities),
                    input_methods=list(guest_status.input_methods),
                    allowed_apps=list(guest_status.apps),
                    detail=guest_status.detail,
                )
                if guest_status is not None
                else GuestToolStatus()
            ),
            queued_actions=controller.queued_jobs if controller else 0,
            controller_owned=True,
            control_owner="operator" if vm_id in self.control.leases else "agent",
            control_state=self.control.leases[vm_id].state
            if vm_id in self.control.leases
            else "available",
            frame_ttl_seconds=instance.profile.frame_ttl_seconds,
            last_error=instance.view.last_error,
            uptime_seconds=(
                round(time.monotonic() - instance.started_monotonic, 3)
                if instance.state is VmState.RUNNING
                else None
            ),
            started_at=instance.view.started_at,
        )
        return StatusResult(
            ok=True,
            status=status,
            capabilities=instance.policy.capabilities(),
        )

    async def observe(
        self,
        vm_id: str,
        *,
        request_id: str | None = None,
        max_long_edge: int | None = None,
        actor: Actor = "agent",
    ) -> ObserveResult:
        instance = await self.manager.require_running(vm_id)
        if max_long_edge is not None and not (64 <= max_long_edge <= 4096):
            raise PaletteError(
                ErrorCode.INVALID_ARGUMENT,
                "max_long_edge must be between 64 and 4096",
                details={"max_long_edge": max_long_edge},
            )
        controller = self._controller(instance)
        frame, captured = await controller.observe(
            request_id=request_id, max_long_edge=max_long_edge
        )
        self._audit("observe", vm_id, frame=frame, scaled=captured.scaled)
        return ObserveResult(
            ok=True,
            frame=frame,
            image_mime_type="image/png",
            image_bytes=captured.byte_count,
            scaled=captured.scaled,
            note=(
                "the image was reduced; coordinates are in the delivered image's pixels"
                if captured.scaled
                else None
            ),
        )

    async def observe_image(
        self, vm_id: str, *, request_id: str | None = None, max_long_edge: int | None = None
    ) -> tuple[ObserveResult, bytes]:
        """Observe and hand back the encoded PNG for the transport layer to embed."""
        instance = await self.manager.require_running(vm_id)
        controller = self._controller(instance)
        frame, captured = await controller.observe(
            request_id=request_id, max_long_edge=max_long_edge
        )
        self._audit("observe", vm_id, frame=frame, scaled=captured.scaled)
        return (
            ObserveResult(
                ok=True,
                frame=frame,
                image_bytes=captured.byte_count,
                scaled=captured.scaled,
                note=(
                    "the image was reduced; coordinates are in the delivered image's pixels"
                    if captured.scaled
                    else None
                ),
            ),
            captured.png_bytes,
        )

    async def wait(
        self,
        vm_id: str,
        *,
        after_frame_id: str | None,
        mode: str,
        timeout_ms: int,
        delay_ms: int | None = None,
        threshold: float = 0.005,
    ) -> tuple[WaitResult, bytes | None]:
        instance = await self.manager.require_running(vm_id)
        if mode not in {"delay", "changed", "stable"}:
            raise PaletteError(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                f"unsupported wait mode {mode!r}",
                details={"supported": ["delay", "changed", "stable"]},
            )
        if not (0 < timeout_ms <= 10_000):
            raise PaletteError(
                ErrorCode.INVALID_ARGUMENT,
                "a single wait must have a timeout between 1 and 10000 ms",
                details={"timeout_ms": timeout_ms},
            )
        if mode in {"changed", "stable"} and after_frame_id is None:
            raise PaletteError(
                ErrorCode.INVALID_ARGUMENT,
                "changed and stable waits compare against a previous frame, so after_frame_id is required",
            )
        controller = self._controller(instance)
        result, image = await controller.wait(
            after_frame_id=after_frame_id,
            mode=mode,
            timeout_ms=timeout_ms,
            delay_ms=delay_ms,
            threshold=threshold,
        )
        self._audit("wait", vm_id, mode=mode, reason=str(result.reason), samples=result.samples)
        return result, image

    # -- actions ----------------------------------------------------------------------
    async def act(
        self,
        vm_id: str,
        *,
        instance_id: str,
        frame_id: str,
        request_id: str,
        action: dict[str, Any],
    ) -> tuple[Receipt, bytes | None]:
        """Run one action and return its receipt plus the post-action image, if any.

        The bytes come from the very capture the receipt names, never from a second one:
        re-capturing would produce a different frame than the one the agent is told to
        reason about.
        """
        validate_identifier(request_id, what="request_id")
        self.control.require_agent(vm_id)
        instance = await self.manager.require_running(vm_id)
        self._require_instance_id(instance, instance_id)
        controller = self._controller(instance)
        return await controller.act(request_id=request_id, frame_id=frame_id, action_payload=action)

    async def release_inputs(self, vm_id: str, *, instance_id: str) -> ReleaseResult:
        self.control.require_agent(vm_id)
        instance = self.manager.get(vm_id)
        self._require_instance_id(instance, instance_id)
        controller = instance.controller
        if controller is None:
            return ReleaseResult(
                ok=True,
                release_confirmed=False,
                note="this VM has no active input session; nothing is held by this service",
            )
        result = await controller.release_inputs()
        self._audit(
            "release_inputs",
            vm_id,
            confirmed=result.release_confirmed,
            keys=len(result.released_keys),
            buttons=len(result.released_buttons),
        )
        return result

    # -- guest enhancement ------------------------------------------------------------
    async def guest_input_text(
        self,
        vm_id: str,
        *,
        instance_id: str,
        boot_epoch: int,
        frame_id: str | None,
        request_id: str,
        text: str,
        method: str,
        target: str = "focused",
    ) -> tuple[GuestToolResult, bytes | None]:
        validate_identifier(request_id, what="request_id")
        self.control.require_agent(vm_id)
        instance = await self.manager.require_running(vm_id)
        self._require_instance_id(instance, instance_id)
        policy = instance.policy
        policy.require_capability("guest.input_text")
        self._require_boot_epoch(instance, boot_epoch)
        backend = instance.guest_tools
        if backend is None:
            raise PaletteError(
                ErrorCode.GUEST_TOOL_UNAVAILABLE, "no guest tool backend is attached"
            )
        controller = self._controller(instance)

        async def runner(on_sending: Callable[[], None]) -> GuestToolOutcome:
            policy.require_capability("guest.input_text")
            return await backend.input_text(
                text=text, method=method, target=target, on_sending=on_sending
            )

        return await controller.run_guest_operation(
            label="guest.input_text",
            runner=runner,
            request_id=request_id,
            frame_id=frame_id,
            parameters={"text": text, "method": method, "target": target},
        )

    async def guest_launch_app(
        self,
        vm_id: str,
        *,
        instance_id: str,
        boot_epoch: int,
        request_id: str,
        app_id: str,
        options: dict[str, Any],
    ) -> tuple[GuestToolResult, bytes | None]:
        validate_identifier(request_id, what="request_id")
        self.control.require_agent(vm_id)
        validate_identifier(app_id, what="app_id")
        instance = await self.manager.require_running(vm_id)
        self._require_instance_id(instance, instance_id)
        policy = instance.policy
        policy.require_capability("guest.launch_app")
        policy.require_app(app_id)
        self._require_boot_epoch(instance, boot_epoch)

        backend = instance.guest_tools
        if backend is None:
            raise PaletteError(
                ErrorCode.GUEST_TOOL_UNAVAILABLE, "no guest tool backend is attached"
            )
        controller = self._controller(instance)

        async def runner(on_sending: Callable[[], None]) -> GuestToolOutcome:
            policy.require_capability("guest.launch_app")
            policy.require_app(app_id)
            return await backend.launch_app(app_id=app_id, options=options, on_sending=on_sending)

        return await controller.run_guest_operation(
            label="guest.launch_app",
            runner=runner,
            request_id=request_id,
            frame_id=None,
            parameters={"app_id": app_id, "options": options},
        )

    def _require_boot_epoch(self, instance: VmInstance, boot_epoch: int) -> None:
        if instance.view.boot_epoch != boot_epoch:
            raise PaletteError(
                ErrorCode.STALE_FRAME,
                "the guest rebooted after this request was prepared",
                details={"expected_boot_epoch": instance.view.boot_epoch},
            )

    # -- lifecycle --------------------------------------------------------------------
    async def start(
        self,
        vm_id: str,
        *,
        profile_id: str | None = None,
        actor: Actor = "operator",
        image: str | None = None,
        image_format: str | None = None,
        iso: Sequence[str] | None = None,
        extra_disks: Sequence[str] | None = None,
        qemu_args: Sequence[str] | None = None,
    ) -> LifecycleResult:
        """Start a VM, optionally with host material the caller supplied.

        Each kind of override has its own switch and its own consequence, so they are
        authorised and validated one at a time rather than as a bundle. Nothing here is
        filtered for content: a caller who may pass QEMU arguments may pass any of them,
        which is exactly the condition the threat model records as "the host boundary is
        the caller's to keep".
        """
        validate_identifier(vm_id, what="vm_id")
        if actor == "agent":
            self.control.require_agent(vm_id)
        profile = self.config.profile(profile_id)
        # The lifecycle gate uses the profile being started, not one already running.
        gate = Policy(profile, _runtime_guard(self.config))
        gate.require_lifecycle("start", actor=actor)
        overrides = self._build_overrides(
            gate,
            actor=actor,
            image=image,
            image_format=image_format,
            iso=iso,
            extra_disks=extra_disks,
            qemu_args=qemu_args,
        )
        instance = await self.manager.start(vm_id, profile.profile_id, overrides=overrides)
        if self.audit.enabled and not overrides.is_empty:
            # Shape only: kinds and counts. The paths themselves go into the manifest, where
            # they belong, and not into a log that outlives the instance.
            self.audit.record(
                "vm.started_with_overrides",
                vm_id=vm_id,
                instance_id=instance.view.instance_id,
                overrides=",".join(overrides.describe()),
            )
        return LifecycleResult(
            ok=True,
            vm_id=vm_id,
            state=str(instance.state),
            instance_id=instance.view.instance_id,
            profile_id=profile.profile_id,
            note=(
                "started with caller-supplied " + ", ".join(overrides.describe())
                if not overrides.is_empty
                else None
            ),
        )

    def _build_overrides(
        self,
        gate: Policy,
        *,
        actor: Actor,
        image: str | None,
        image_format: str | None,
        iso: Sequence[str] | None,
        extra_disks: Sequence[str] | None,
        qemu_args: Sequence[str] | None,
    ) -> LaunchOverrides:
        """Authorise, then validate, then normalise what the caller handed us."""
        resolved_image: Path | None = None
        if image is not None:
            gate.require_host_material("image", actor=actor)
            resolved_image = validate_caller_path(image, what="image")
        if image_format is not None and resolved_image is None:
            raise PaletteError(
                ErrorCode.INVALID_ARGUMENT,
                "image_format only means something together with image",
            )
        if image_format is not None:
            image_format = image_format.lower()
            if image_format not in {"raw", "qcow2"}:
                raise PaletteError(ErrorCode.IMAGE_INVALID, "image_format must be raw or qcow2")

        volumes: list[ExtraVolume] = []
        optical = list(iso or ())
        disks = list(extra_disks or ())
        if optical or disks:
            gate.require_host_material("iso", actor=actor)
        for path in optical:
            volumes.append(
                ExtraVolume(path=str(validate_caller_path(path, what="ISO")), media="cdrom")
            )
        for path in disks:
            volumes.append(
                ExtraVolume(path=str(validate_caller_path(path, what="extra disk")), media="disk")
            )

        args = list(qemu_args or ())
        if args:
            gate.require_host_material("qemu_args", actor=actor)
            args = _validate_qemu_args(args)

        return LaunchOverrides(
            image=resolved_image,
            image_format=image_format,
            volumes=tuple(volumes),
            qemu_args=tuple(args),
        )

    async def stop(
        self, vm_id: str, *, force: bool = False, actor: Actor = "operator"
    ) -> LifecycleResult:
        if actor == "agent":
            self.control.require_agent(vm_id)
        else:
            await self.control.revoke(vm_id)
        instance = self.manager.get(vm_id)
        instance.policy.require_lifecycle("force_stop" if force else "stop", actor=actor)
        stopped = await self.manager.stop(
            vm_id, force=force, expected_instance_id=instance.view.instance_id
        )
        return LifecycleResult(
            ok=True,
            vm_id=vm_id,
            state=str(stopped.state),
            instance_id=stopped.view.instance_id,
        )

    async def reset(
        self, vm_id: str, *, profile_id: str | None = None, actor: Actor = "operator"
    ) -> LifecycleResult:
        if actor == "agent":
            self.control.require_agent(vm_id)
        else:
            await self.control.revoke(vm_id)
        instance = self.manager.find(vm_id)
        profile = instance.profile if instance is not None else self.config.profile(profile_id)
        gate = Policy(profile, _runtime_guard(self.config))
        gate.require_lifecycle("reset", actor=actor)
        new_instance = await self.manager.reset(
            vm_id,
            profile.profile_id,
            expected_instance_id=instance.view.instance_id if instance is not None else None,
            expect_absent=instance is None,
        )
        return LifecycleResult(
            ok=True,
            vm_id=vm_id,
            state=str(new_instance.state),
            instance_id=new_instance.view.instance_id,
            profile_id=profile.profile_id,
            note=(
                "the previous writable layer was handled by the retention policy; "
                "the base image is untouched"
            ),
        )

    async def emergency_stop(self, vm_id: str | None = None) -> list[str]:
        """Operator escape hatch: release inputs and stop, without a graceful guest."""
        if vm_id is not None:
            instance = self.manager.get(vm_id)
            await self.manager.stop(
                vm_id, force=True, expected_instance_id=instance.view.instance_id
            )
            return [vm_id]
        return await self.manager.emergency_stop_all()

    async def take_control(self, vm_id: str, *, reason: str) -> int:
        """Human/operator takeover: invalidate every frame and bump the control epoch.

        The plan requires an epoch bump on handover so that frames captured under the
        previous controller cannot be used to drive input afterwards.
        """
        instance = self.manager.get(vm_id)
        epoch = await self.manager.take_control(
            vm_id, expected_instance_id=instance.view.instance_id
        )
        self._audit("control.handover", vm_id, epoch=epoch, reason=reason)
        return epoch

    # -- audit ------------------------------------------------------------------------
    def _audit(self, event: str, vm_id: str, **fields: Any) -> None:
        if not self.audit.enabled:
            return
        payload: dict[str, Any] = {"vm_id": vm_id, "event_kind": event}
        frame = fields.pop("frame", None)
        if isinstance(frame, Frame):
            payload["instance_id"] = frame.instance_id
            payload["frame_epoch"] = frame.display_epoch
        payload.update({key: value for key, value in fields.items() if _safe_audit_value(value)})
        self.audit.record(f"tool.{event}", **payload)


#: Bounds on caller-supplied QEMU arguments. Deliberately not a content filter — a caller
#: who may pass arguments may pass any of them, including ones that add a monitor channel or
#: point QEMU at a host file. These limits only stop a broken or absurd vector before a
#: process is spawned.
MAX_CALLER_ARGS = 64
MAX_CALLER_ARG_CHARS = 4096


def _validate_qemu_args(args: Sequence[str]) -> list[str]:
    if len(args) > MAX_CALLER_ARGS:
        raise PaletteError(
            ErrorCode.INVALID_ARGUMENT,
            f"at most {MAX_CALLER_ARGS} QEMU arguments may be supplied per start",
            details={"count": len(args)},
        )
    cleaned: list[str] = []
    for argument in args:
        if not isinstance(argument, str) or not argument:
            raise PaletteError(
                ErrorCode.INVALID_ARGUMENT, "QEMU arguments must be non-empty strings"
            )
        if "\x00" in argument:
            raise PaletteError(
                ErrorCode.INVALID_ARGUMENT, "a QEMU argument must not contain a NUL byte"
            )
        if len(argument) > MAX_CALLER_ARG_CHARS:
            raise PaletteError(
                ErrorCode.INVALID_ARGUMENT,
                f"a single QEMU argument may be at most {MAX_CALLER_ARG_CHARS} characters",
            )
        cleaned.append(argument)
    return cleaned


def _safe_audit_value(value: Any) -> bool:
    return isinstance(value, str | int | float | bool | type(None))


def _runtime_guard(config: PaletteConfig) -> PathGuard:
    from .policy import PathGuard

    return PathGuard(config.runtime_root)
