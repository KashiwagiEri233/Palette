"""Application lifecycle rules shared by MCP, desktop and operator adapters."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import ErrorCode, PaletteError
from .models import LifecycleResult
from .policy import PathGuard, Policy, validate_identifier
from .vm.images import validate_caller_path
from .vm.launcher import ExtraVolume, LaunchOverrides
from .vm.manager import ShutdownMode

if TYPE_CHECKING:
    from .desktop import DesktopManager
    from .service import Actor, PaletteService


class LifecycleService:
    def __init__(self, service: PaletteService) -> None:
        self.record_event = service.record_event
        self.config = service.config
        self.manager = service.manager
        self.control = service.control
        self.driver = service.driver
        self.audit = service.audit
        self.desktop: DesktopManager | None = None

    def _audit(self, event: str, vm_id: str, **fields: Any) -> None:
        self.record_event(event, vm_id, **fields)

    def is_persistent(self, vm_id: str) -> bool:
        return self.desktop is not None and vm_id in self.desktop.definitions

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
        async with self.manager.operations.hold(vm_id, starting=True):
            validate_identifier(vm_id, what="vm_id")
            if self.is_persistent(vm_id):
                assert self.desktop is not None  # noqa: S101
                definition = self.desktop.get(vm_id)
                if actor == "agent":
                    self.control.require_agent(vm_id)
                Policy(definition.profile, PathGuard(self.config.runtime_root)).require_lifecycle(
                    "start", actor=actor
                )
                if profile_id not in (None, definition.draft.profile_id):
                    raise PaletteError(
                        ErrorCode.INVALID_ARGUMENT,
                        "persistent VM profile is configured by the operator",
                    )
                if any(
                    value is not None
                    for value in (image, image_format, iso, extra_disks, qemu_args)
                ):
                    raise PaletteError(
                        ErrorCode.INVALID_ARGUMENT,
                        "persistent VM storage is configured by the operator",
                    )
                return LifecycleResult(ok=True, **await self.desktop.start(vm_id))
            if actor == "agent":
                self.control.require_agent(vm_id)
            profile = self.config.profile(profile_id)
            # The lifecycle gate uses the profile being started, not one already running.
            gate = Policy(profile, PathGuard(self.config.runtime_root))
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
        self,
        vm_id: str,
        *,
        force: bool = False,
        actor: Actor = "operator",
        expected_instance_id: str | None = None,
    ) -> LifecycleResult:
        authorized = self.manager.find(vm_id)
        expected = expected_instance_id or (authorized.view.instance_id if authorized else None)
        async with self.manager.operations.hold(vm_id):
            self.manager.check_identity(self.manager.find(vm_id), expected)
            if self.is_persistent(vm_id):
                assert self.desktop is not None  # noqa: S101
                definition = self.desktop.get(vm_id)
                if actor == "agent":
                    self.control.require_agent(vm_id)
                Policy(definition.profile, PathGuard(self.config.runtime_root)).require_lifecycle(
                    "force_stop" if force else "stop", actor=actor
                )
                return LifecycleResult(
                    ok=True,
                    **await self.desktop.stop(vm_id, force=force, expected_instance_id=expected),
                )
            instance = self.manager.get(vm_id)
            instance.policy.require_lifecycle("force_stop" if force else "stop", actor=actor)
            if actor == "agent":
                self.control.require_agent(vm_id)
            else:
                await self.control.revoke(vm_id)
            await self.manager.shutdown_instance(
                vm_id,
                mode=ShutdownMode.FORCE if force else ShutdownMode.WITH_FALLBACK,
                expected_instance_id=expected or instance.view.instance_id,
            )
            return LifecycleResult(
                ok=True,
                vm_id=vm_id,
                state=str(instance.state),
                instance_id=instance.view.instance_id,
            )

    async def reset(
        self, vm_id: str, *, profile_id: str | None = None, actor: Actor = "operator"
    ) -> LifecycleResult:
        authorized = self.manager.find(vm_id)
        expected = authorized.view.instance_id if authorized else None
        async with self.manager.operations.hold(vm_id, starting=True):
            self.manager.check_identity(
                self.manager.find(vm_id), expected, expect_absent=authorized is None
            )
            if self.is_persistent(vm_id):
                assert self.desktop is not None  # noqa: S101
                definition = self.desktop.get(vm_id)
                if actor == "agent":
                    self.control.require_agent(vm_id)
                Policy(definition.profile, PathGuard(self.config.runtime_root)).require_lifecycle(
                    "reset", actor=actor
                )
                return LifecycleResult(ok=True, **await self.desktop.reset(vm_id))
            instance = self.manager.find(vm_id)
            profile = instance.profile if instance is not None else self.config.profile(profile_id)
            gate = Policy(profile, PathGuard(self.config.runtime_root))
            gate.require_lifecycle("reset", actor=actor)
            if actor == "agent":
                self.control.require_agent(vm_id)
            else:
                await self.control.revoke(vm_id)
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
        async with self.manager.operations.hold(vm_id):
            instance = self.manager.get(vm_id)
            if not await self.driver.revoke(vm_id):
                raise PaletteError(
                    ErrorCode.QMP_UNAVAILABLE, "Agent input release was not confirmed"
                )
            epoch = await self.manager.take_control(
                vm_id, expected_instance_id=instance.view.instance_id
            )
            self._audit("control.handover", vm_id, epoch=epoch, reason=reason)
            return epoch


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
