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

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from typing import Any

from .audit import AuditLog, NullAuditLog
from .config import PaletteConfig
from .contracts import Actor as Actor
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
from .policy import Policy, validate_identifier
from .vm.manager import VmInstance, VmManager, VmState

logger = logging.getLogger("palette.service")

#: Roles a request can be attributed to. Only trusted call sites choose a role; an agent
#: cannot ask for one.


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
        from .lifecycle import LifecycleService

        self.lifecycle = LifecycleService(self)
        self._shutdown_task: asyncio.Task[None] | None = None

    # -- lifecycle --------------------------------------------------------------------
    async def startup(self) -> None:
        self.config.runtime_root.mkdir(parents=True, exist_ok=True)
        self.control.start()

    async def shutdown(self) -> None:
        from .cleanup import close_all, join_cleanup

        if self._shutdown_task is None:
            self._shutdown_task = asyncio.create_task(
                close_all(
                    (self.manager.operations.seal, self.control.close, self.manager.shutdown)
                ),
                name="service-shutdown",
            )
        await join_cleanup(self._shutdown_task)

    # -- helpers ----------------------------------------------------------------------
    def policy_for(self, vm_id: str) -> Policy:
        return self.manager.get(vm_id).policy

    def _instance(self, vm_id: str) -> VmInstance:
        return self.manager.get(vm_id)

    def require_instance_id(self, instance: VmInstance, instance_id: str) -> None:
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

    def controller_for(self, instance: VmInstance) -> ActionController:
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
        controller = self.controller_for(instance)
        frame, captured = await controller.observe(
            request_id=request_id, max_long_edge=max_long_edge
        )
        self.record_event("observe", vm_id, frame=frame, scaled=captured.scaled)
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
        controller = self.controller_for(instance)
        frame, captured = await controller.observe(
            request_id=request_id, max_long_edge=max_long_edge
        )
        self.record_event("observe", vm_id, frame=frame, scaled=captured.scaled)
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
        controller = self.controller_for(instance)
        result, image = await controller.wait(
            after_frame_id=after_frame_id,
            mode=mode,
            timeout_ms=timeout_ms,
            delay_ms=delay_ms,
            threshold=threshold,
        )
        self.record_event(
            "wait", vm_id, mode=mode, reason=str(result.reason), samples=result.samples
        )
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
        self.require_instance_id(instance, instance_id)
        controller = self.controller_for(instance)
        return await controller.act(request_id=request_id, frame_id=frame_id, action_payload=action)

    async def release_inputs(self, vm_id: str, *, instance_id: str) -> ReleaseResult:
        self.control.require_agent(vm_id)
        instance = self.manager.get(vm_id)
        self.require_instance_id(instance, instance_id)
        controller = instance.controller
        if controller is None:
            return ReleaseResult(
                ok=True,
                release_confirmed=False,
                note="this VM has no active input session; nothing is held by this service",
            )
        result = await controller.release_inputs()
        self.record_event(
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
        self.require_instance_id(instance, instance_id)
        policy = instance.policy
        policy.require_capability("guest.input_text")
        self._require_boot_epoch(instance, boot_epoch)
        backend = instance.guest_tools
        if backend is None:
            raise PaletteError(
                ErrorCode.GUEST_TOOL_UNAVAILABLE, "no guest tool backend is attached"
            )
        controller = self.controller_for(instance)

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
        self.require_instance_id(instance, instance_id)
        policy = instance.policy
        policy.require_capability("guest.launch_app")
        policy.require_app(app_id)
        self._require_boot_epoch(instance, boot_epoch)

        backend = instance.guest_tools
        if backend is None:
            raise PaletteError(
                ErrorCode.GUEST_TOOL_UNAVAILABLE, "no guest tool backend is attached"
            )
        controller = self.controller_for(instance)

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
        return await self.lifecycle.start(
            vm_id,
            profile_id=profile_id,
            actor=actor,
            image=image,
            image_format=image_format,
            iso=iso,
            extra_disks=extra_disks,
            qemu_args=qemu_args,
        )

    async def stop(
        self,
        vm_id: str,
        *,
        force: bool = False,
        actor: Actor = "operator",
        expected_instance_id: str | None = None,
    ) -> LifecycleResult:
        return await self.lifecycle.stop(
            vm_id, force=force, actor=actor, expected_instance_id=expected_instance_id
        )

    async def reset(
        self, vm_id: str, *, profile_id: str | None = None, actor: Actor = "operator"
    ) -> LifecycleResult:
        return await self.lifecycle.reset(vm_id, profile_id=profile_id, actor=actor)

    async def emergency_stop(self, vm_id: str | None = None) -> list[str]:
        return await self.lifecycle.emergency_stop(vm_id)

    async def take_control(self, vm_id: str, *, reason: str) -> int:
        return await self.lifecycle.take_control(vm_id, reason=reason)

    # -- audit ------------------------------------------------------------------------
    def record_event(self, event: str, vm_id: str, **fields: Any) -> None:
        if not self.audit.enabled:
            return
        payload: dict[str, Any] = {"vm_id": vm_id, "event_kind": event}
        frame = fields.pop("frame", None)
        if isinstance(frame, Frame):
            payload["instance_id"] = frame.instance_id
            payload["frame_epoch"] = frame.display_epoch
        payload.update({key: value for key, value in fields.items() if _safe_audit_value(value)})
        self.audit.record(f"tool.{event}", **payload)


def _safe_audit_value(value: Any) -> bool:
    return isinstance(value, str | int | float | bool | type(None))
