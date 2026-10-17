"""The bounded action transaction engine: one writer per VM.

Everything that changes the desktop — QMP input, future guest-tool input, app launches —
goes through this one scheduler, so a VM never has two things pushing at it at once. The
transaction shape is fixed (plan §8.1):

    check authority -> validate frame -> send input -> settle -> capture -> receipt

and every stage contributes an honest value to ``effect_state``:

* ``not_sent``  — nothing with a side effect left the service;
* ``submitted`` — QEMU accepted the input; the guest may still be ignoring it;
* ``partial``   — some of the planned input was delivered;
* ``unknown``   — a timeout or a disconnect means nobody can say what happened.

``unknown`` and ``partial`` never trigger an automatic retry. The plan is explicit that a
lost response may correspond to a click that already landed, so the receipt reports the
ambiguity and leaves the decision to the agent.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from ..audit import AuditLog
from ..capture.base import CaptureBackend, CapturedImage
from ..cleanup import join_cleanup
from ..config import Profile
from ..errors import ErrorCode, PaletteError
from ..guest_tools.base import GuestToolOutcome
from ..models import (
    Action,
    ActionSource,
    EffectState,
    ErrorPayload,
    Frame,
    GuestToolResult,
    KeyAction,
    ObservationState,
    Receipt,
    ReleaseResult,
    TypeTextAction,
    Viewport,
    WaitReason,
    WaitResult,
    parse_action,
)
from ..qmp.client import QmpClient
from .coordinates import CoordinateTransform, transform_id_for
from .dedupe import RequestDeduplicator
from .events import InputPlanner, button_event, key_event
from .frames import (
    FrameRegistry,
    image_difference_ratio,
    image_thumbnail,
    rescale,
    thumbnail_difference,
)
from .ledger import InputLedger
from .scheduler import ActionScheduler, Job, JobKind
from .state import ActionResult, CursorPositionSource, InstanceView

logger = logging.getLogger("palette.input")

#: A job may wait this long for the queue before the service reports BUSY.
DEFAULT_QUEUE_WAIT_SECONDS = 5.0

#: Pixel-difference heuristic settings for ``vm_wait``.
_STABLE_SAMPLE_INTERVAL = 0.25
_THUMBNAIL_SIZE = (64, 64)


# --------------------------------------------------------------------------------------
# supporting structures
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class _InputProgress:
    """Evidence for one transaction, retained even if its caller disappears."""

    view: InstanceView
    sequence: int
    attempted: bool = False
    in_flight: bool = False
    accepted: int = 0
    complete: bool = False

    def sending(self) -> None:
        # Reserve once at the first possible delivery, including a write whose reply is lost.
        if not self.attempted:
            self.view.action_seq += 1
            self.sequence = self.view.action_seq
        self.attempted = True
        self.in_flight = True

    @property
    def effect(self) -> EffectState:
        if self.complete:
            return EffectState.SUBMITTED
        if self.in_flight:
            return EffectState.UNKNOWN
        return EffectState.PARTIAL if self.accepted else EffectState.NOT_SENT


# --------------------------------------------------------------------------------------
# the controller
# --------------------------------------------------------------------------------------


class ActionController:
    """Serialises observation and input for exactly one VM instance."""

    def __init__(
        self,
        *,
        view: InstanceView,
        profile: Profile,
        qmp: QmpClient,
        capture: CaptureBackend,
        frames_dir: Path,
        audit: AuditLog | None = None,
    ) -> None:
        self.view = view
        self.profile = profile
        self._qmp = qmp
        self._capture = capture
        self._frames_dir = frames_dir
        self._audit = audit
        self._ledger = InputLedger()
        self._frames = FrameRegistry()
        self._dedupe = RequestDeduplicator()
        self._generation = 0
        self._last_capture_at: float = 0.0
        self.mutation_guard: Callable[[], None] | None = None
        self._shutdown_task: asyncio.Task[ReleaseResult | None] | None = None
        self._planner = InputPlanner(profile)
        self.scheduler = ActionScheduler(
            profile,
            guard=self._check_mutation,
            release=self._bounded_release,
            is_clean=lambda: self._ledger.is_clean,
        )

    # -- lifecycle -------------------------------------------------------------------
    def _check_mutation(self) -> None:
        if self.mutation_guard is not None:
            self.mutation_guard()

    async def start(self) -> None:
        await self.scheduler.start(self.view.vm_id)

    async def shutdown(self, *, release: bool = True) -> ReleaseResult | None:
        if self._shutdown_task is None:
            self._shutdown_task = asyncio.create_task(
                self._shutdown(release=release), name="input-shutdown"
            )
        return await join_cleanup(self._shutdown_task)

    async def _shutdown(self, *, release: bool) -> ReleaseResult | None:
        """Stop accepting work, drop queued jobs, and try to leave the guest unheld.

        The worker finishes its independent cleanup before the final release and shutdown.
        """
        self.scheduler.closing = True
        self.invalidate(reason="the VM is shutting down")
        current = self.scheduler.current
        if current is not None:
            with contextlib.suppress(PaletteError):
                await asyncio.shield(current.future)
        result = await self._bounded_release() if release else None
        await self.scheduler.close()
        self._frames.clear(reason="the VM is shutting down")
        return result

    def invalidate(self, *, reason: str) -> None:
        """Fence in-flight captures and cancel old work before yielding to the event loop."""
        self._generation += 1
        self._frames.clear(reason=reason)
        error = PaletteError(ErrorCode.STALE_FRAME, reason, retryable=False)
        self.scheduler.cancel_all(error)

    # -- public API ------------------------------------------------------------------
    async def observe(
        self,
        *,
        request_id: str | None = None,
        max_long_edge: int | None = None,
    ) -> tuple[Frame, CapturedImage]:
        """Take a fresh capture, queued so it cannot observe a half-finished action."""
        job = self.scheduler.make_job(
            JobKind.OBSERVE,
            "observe",
            lambda: self._capture_frame(max_long_edge=max_long_edge),
        )
        return cast("tuple[Frame, CapturedImage]", await self.scheduler.submit(job))

    async def act(
        self,
        *,
        request_id: str,
        frame_id: str,
        action_payload: dict[str, Any],
    ) -> tuple[Receipt, bytes | None]:
        """Run one action and return its receipt with the post-action image, if produced.

        The bytes belong to the frame named in the receipt. A de-duplicated replay returns
        the original receipt with no image, which the plan allows: re-running the input to
        regenerate a picture would be far worse than asking the agent to observe again.
        """
        action = parse_action(action_payload)
        fingerprint = _fingerprint(action_payload, frame_id)

        progress = _InputProgress(self.view, self.view.action_seq)
        job = self.scheduler.make_job(
            JobKind.ACTION,
            f"action:{action.type}",
            lambda: self._run_action(
                request_id=request_id, frame_id=frame_id, action=action, progress=progress
            ),
        )

        def interrupted(error: PaletteError) -> tuple[Receipt, None]:
            self._record_action(
                "action.interrupted", action, request_id, error.code, progress.accepted
            )
            return _receipt(
                ok=False,
                request_id=request_id,
                action=action,
                effect_state=progress.effect,
                action_seq=progress.sequence,
                error=error,
                input_submitted=progress.accepted > 0,
                observation=ObservationState.FAILED
                if progress.complete
                else ObservationState.SKIPPED,
                frame_observation_required=progress.attempted,
                backend=self._backend_label(action),
            ), None

        job.abort_result = interrupted
        return cast(
            "tuple[Receipt, bytes | None]",
            await self._submit_action(request_id, fingerprint, job),
        )

    async def wait(
        self,
        *,
        after_frame_id: str | None,
        mode: str,
        timeout_ms: int,
        delay_ms: int | None = None,
        threshold: float = 0.005,
    ) -> tuple[WaitResult, bytes | None]:
        """Bounded waiting, returning the result with its last sample's bytes.

        The bytes and the frame in the result always come from the *same* capture; a tool
        that paired a picture with another moment's metadata would be worse than sending no
        picture at all.

        Each sample briefly takes the action slot and then releases it, so a long wait
        never blocks an operator's emergency release. Pixel change and pixel stability are
        heuristics only — a blinking cursor or an animation can satisfy either without the
        application being ready, and the result says so.
        """
        started = time.monotonic()
        deadline = started + timeout_ms / 1000.0
        samples = 0
        baseline: bytes | None = None
        previous: bytes | None = None

        if mode == "delay":
            wait_seconds = min((delay_ms or timeout_ms) / 1000.0, timeout_ms / 1000.0)
            await asyncio.sleep(wait_seconds)
            frame, captured = await self.observe()
            return (
                WaitResult(
                    ok=True,
                    reason=WaitReason.DELAY_ELAPSED,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    samples=0,
                    frame=frame,
                    note="delayed without comparing pixels; this says nothing about readiness",
                ),
                captured.png_bytes,
            )

        if after_frame_id is not None:
            base_frame = self._frames.get(after_frame_id)
            self._check_frame_authority(base_frame)
            # Compare against the picture the agent actually looked at, not against a
            # fresh sample taken after the wait began. Otherwise "has it changed?" would
            # silently mean "has it changed since I started waiting?" and the answer would
            # always be no for a change that happened in between.
            baseline = self._frames.thumbnail(after_frame_id)
        if baseline is None:
            _frame, captured_baseline = await self.observe()
            baseline = image_thumbnail(captured_baseline.png_bytes)
            samples += 1
        previous = baseline

        last_frame: Frame | None = None
        last_bytes: bytes | None = None
        last_ratio: float | None = None
        stable_streak = 0

        while time.monotonic() < deadline:
            await asyncio.sleep(_STABLE_SAMPLE_INTERVAL)
            frame, captured = await self.observe()
            last_frame = frame
            last_bytes = captured.png_bytes
            samples += 1
            current = self._frames.thumbnail(frame.frame_id) or image_thumbnail(captured.png_bytes)

            ratio = thumbnail_difference(baseline, current)
            last_ratio = ratio
            if mode == "changed" and ratio > threshold:
                return WaitResult(
                    ok=True,
                    reason=WaitReason.CONTENT_CHANGED,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    samples=samples,
                    changed_ratio=ratio,
                    frame=frame,
                    note="pixel change detected; it does not prove the application finished",
                ), captured.png_bytes
            if mode == "stable":
                pair_ratio = thumbnail_difference(previous, current)
                stable_streak = stable_streak + 1 if pair_ratio <= threshold else 0
                if stable_streak >= 2:
                    return WaitResult(
                        ok=True,
                        reason=WaitReason.CONTENT_STABLE,
                        elapsed_ms=int((time.monotonic() - started) * 1000),
                        samples=samples,
                        changed_ratio=pair_ratio,
                        frame=frame,
                        note="the screen stopped changing; clocks and blinking cursors can defeat this",
                    ), captured.png_bytes
            previous = current

        if last_frame is None:
            last_frame, captured = await self.observe()
            last_bytes = captured.png_bytes
        return (
            WaitResult(
                ok=True,
                reason=WaitReason.TIMEOUT,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                samples=samples,
                changed_ratio=last_ratio,
                frame=last_frame,
                note="the wait timed out; the desktop may still be working",
            ),
            last_bytes,
        )

    async def run_guest_operation(
        self,
        *,
        label: str,
        request_id: str,
        frame_id: str | None,
        parameters: dict[str, Any],
        runner: Callable[[Callable[[], None]], Awaitable[GuestToolOutcome]],
    ) -> tuple[GuestToolResult, bytes | None]:
        """Run and retain guest receipts using the same queue and identity as visual input."""
        identity = self._identity()
        progress = _InputProgress(self.view, self.view.action_seq)

        def failure(error: PaletteError) -> tuple[GuestToolResult, None]:
            self._record_guest(label, None, error.code)
            return GuestToolResult(
                ok=False,
                error=ErrorPayload(**error.as_payload()),
                effect_state=progress.effect,
                request_id=request_id,
                action_seq=progress.sequence,
                detail=error.message,
            ), None

        async def _job() -> tuple[GuestToolResult, bytes | None]:
            try:
                self._require_identity(identity)
                if frame_id is not None:
                    self._validate_frame_for_action(self._frames.get(frame_id))
                    await self._ensure_size_is_current()
                    self._validate_frame_for_action(self._requeue_or_stale(frame_id))
                result = await runner(progress.sending)
                self._require_identity(identity)
            except PaletteError as exc:
                return failure(exc)
            progress.in_flight = result.effect_state is EffectState.UNKNOWN
            progress.complete = result.effect_state is EffectState.SUBMITTED
            progress.accepted = int(
                result.effect_state in {EffectState.SUBMITTED, EffectState.PARTIAL}
            )
            settle = self.profile.timeouts.settle_after_input_ms / 1000.0
            if settle:
                await asyncio.sleep(settle)
            frame: Frame | None = None
            png: bytes | None = None
            try:
                frame, captured = await self._capture_frame()
                png = captured.png_bytes
                self.view.screen_ready = True
            except PaletteError as exc:
                logger.info("post-guest-operation capture failed: %s", exc.code)
            error = result.error
            if not result.ok and error is None:
                error = PaletteError(
                    ErrorCode.GUEST_TOOL_UNAVAILABLE,
                    result.detail or "the guest operation failed",
                    retryable=False,
                )
            self._record_guest(label, frame is not None, error.code if error else None)
            return GuestToolResult(
                ok=result.ok,
                error=ErrorPayload(**error.as_payload()) if error else None,
                effect_state=result.effect_state,
                method=result.method,
                launch_id=result.launch_id,
                request_id=request_id,
                action_seq=progress.sequence,
                frame=frame,
                detail=result.detail,
            ), png

        job = self.scheduler.make_job(JobKind.ACTION, label, _job)
        job.abort_result = failure
        return cast(
            "tuple[GuestToolResult, bytes | None]",
            await self._submit_action(
                request_id, _fingerprint({"operation": label, **parameters}, frame_id), job
            ),
        )

    def _record_guest(self, label: str, observed: bool | None, code: ErrorCode | None) -> None:
        if not self._audit:
            return
        payload: dict[str, Any] = {
            "vm_id": self.view.vm_id,
            "instance_id": self.view.instance_id,
            "operation": label,
            "source": "guest_tool",
            "action_seq": self.view.action_seq,
        }
        if observed is not None:
            payload["observed"] = observed
        if code is not None:
            payload["error_code"] = str(code)
        self._audit.record("guest.operation", **payload)

    async def release_inputs(self, *, guarded: bool = False) -> ReleaseResult:
        """Release every key and button the service believes it holds."""

        async def release() -> ReleaseResult:
            if guarded:
                self._check_mutation()
            return await self._release_all()

        job = self.scheduler.make_job(JobKind.RELEASE, "release", release)
        with contextlib.suppress(PaletteError):
            return cast(
                ReleaseResult,
                await self.scheduler.submit(job, queue_wait=DEFAULT_QUEUE_WAIT_SECONDS),
            )
        # A release that could not even be queued still has to report the truth.
        return ReleaseResult(
            ok=False,
            release_confirmed=False,
            note="release could not be scheduled; the input ledger is unchanged",
            released_keys=list(self._ledger.keys),
            released_buttons=list(self._ledger.buttons),
        )

    async def emergency_release(self, *, invalidate: bool = True) -> ReleaseResult:
        """Cancel whatever is running, then release. Used by stop/reset and by the CLI.

        The cleanup runs as its own task so that cancelling the action cannot cancel the
        very coroutine responsible for letting go of the mouse button.
        """
        if invalidate:
            self.invalidate(reason="input was interrupted for recovery")
        current = self.scheduler.current
        if current is not None:
            with contextlib.suppress(PaletteError):
                await asyncio.shield(current.future)
        return await self.release_inputs()

    # -- introspection ---------------------------------------------------------------
    def ledger_snapshot(self) -> dict[str, list[str]]:
        return self._ledger.snapshot()

    def frame_by_id(self, frame_id: str) -> Frame:
        """Public access to the frame registry, used by the service for guest calls."""
        return self._frames.get(frame_id)

    def clear_frames(self, *, reason: str = "control changed hands") -> None:
        """Retire every frame, used when control changes hands."""
        self._frames.clear(reason=reason)

    @property
    def queued_jobs(self) -> int:
        return self.scheduler.queued_jobs

    @property
    def latest_frame(self) -> Frame | None:
        return self._frames.latest()

    @property
    def frame_ttl_seconds(self) -> float:
        return self.profile.frame_ttl_seconds

    # -- scheduler -------------------------------------------------------------------
    def _identity(self) -> tuple[str, int, int, int]:
        return (
            self.view.instance_id,
            self.view.boot_epoch,
            self.view.controller_epoch,
            self._generation,
        )

    def _require_identity(self, identity: tuple[str, int, int, int]) -> None:
        if identity != self._identity():
            raise PaletteError(ErrorCode.STALE_FRAME, "the VM changed during this operation")

    async def _submit_action(
        self, request_id: str, fingerprint: str, job: Job
    ) -> tuple[ActionResult, bytes | None]:
        key = (self.view.instance_id, self.view.boot_epoch, self.view.controller_epoch, request_id)
        cached = self._dedupe.lookup(key, fingerprint)
        if cached is not None:
            return cached, None
        inflight = self._dedupe.begin(key, fingerprint)
        if inflight is not None:
            result, _png = await asyncio.shield(inflight)
            return result.model_copy(update={"deduplicated": True}), None
        self._dedupe.attach(key, fingerprint, job.future)
        job.completed = lambda result: self._dedupe.complete(key, fingerprint, result[0])
        try:
            return cast("tuple[ActionResult, bytes | None]", await self.scheduler.submit(job))
        except PaletteError:
            if not job.future.done():
                self._dedupe.abandon(key)
            raise

    # -- capture ---------------------------------------------------------------------
    async def _capture_frame(
        self,
        *,
        max_long_edge: int | None = None,
        cursor_position_source: CursorPositionSource = "not_observed",
    ) -> tuple[Frame, CapturedImage]:
        identity = self._identity()
        sink = self._frame_path()
        captured = await self._capture.capture(
            display_id=self.view.display_id, head=self.view.head, sink=sink
        )
        self._require_identity(identity)
        if max_long_edge is not None and max_long_edge != self.profile.capture.max_long_edge:
            captured = rescale(captured, max_long_edge, self.profile.capture.max_bytes)
        frame = self._build_frame(captured, cursor_position_source=cursor_position_source)
        self._last_capture_at = time.monotonic()
        thumbnail = image_thumbnail(captured.png_bytes)
        return self._frames.add(frame, thumbnail), captured

    def _frame_path(self) -> Path:
        # Frames live under the instance directory. When capture.keep_frames is off the
        # backend reads and deletes the file immediately, so this is a bounded scratch
        # area rather than an accumulating archive of the agent's screens.
        return self._frames_dir / f"frame-{time.monotonic_ns()}.png"

    def _build_frame(
        self, captured: CapturedImage, *, cursor_position_source: CursorPositionSource
    ) -> Frame:
        view = self.view
        previous_size = view.native_size
        # The view follows what was actually captured, *always*. Leaving the old size in
        # place after a change is not a cosmetic staleness: every later action compares its
        # frame against this value, so a single resize would refuse every subsequent action
        # for the rest of the instance's life. A guest that starts at the firmware's
        # 640x480 and switches to its real mode makes that the normal path, not an edge case.
        view.native_size = captured.native_size
        if previous_size is not None and previous_size != captured.native_size:
            # Every frame captured *before* this moment maps to a surface that no longer
            # exists, so they are retired here rather than relying on each caller to notice.
            # The frame being built right now carries the new size and stays usable.
            view.display_epoch += 1
            self._frames.clear(reason="the display resolution changed")
            logger.info(
                "display size changed for %s: %sx%s -> %sx%s (display_epoch=%d)",
                view.vm_id,
                previous_size.width,
                previous_size.height,
                captured.native_size.width,
                captured.native_size.height,
                view.display_epoch,
            )
            if self._audit:
                self._audit.record(
                    "display.resized",
                    vm_id=view.vm_id,
                    instance_id=view.instance_id,
                    display_epoch=view.display_epoch,
                )
        elif previous_size is None:
            expected = self.profile.display
            if (captured.native_size.width, captured.native_size.height) != (
                expected.width,
                expected.height,
            ):
                logger.warning(
                    "framebuffer is %sx%s but the profile expects %sx%s; the guest template "
                    "does not match its profile",
                    captured.native_size.width,
                    captured.native_size.height,
                    expected.width,
                    expected.height,
                )

        return Frame(
            frame_id=Frame.new_id(),
            vm_id=view.vm_id,
            instance_id=view.instance_id,
            boot_epoch=view.boot_epoch,
            display_id=view.display_id,
            head=view.head,
            display_epoch=view.display_epoch,
            controller_epoch=view.controller_epoch,
            action_seq=view.action_seq,
            native_size=captured.native_size,
            image_size=captured.image_size,
            viewport=Viewport.full(captured.native_size),
            transform_id=transform_id_for(captured.image_size, captured.native_size),
            captured_at=datetime.now().astimezone(),
            monotonic_stamp=time.monotonic(),
            cursor_position_source=cursor_position_source,
        )

    async def _sample_bytes(self) -> bytes:
        _frame, captured = await self._capture_frame()
        return captured.png_bytes

    # -- validation ------------------------------------------------------------------
    def _check_frame_authority(self, frame: Frame) -> None:
        view = self.view
        if frame.vm_id != view.vm_id or frame.instance_id != view.instance_id:
            raise PaletteError(
                ErrorCode.STALE_FRAME,
                "this frame belongs to a different VM instance",
            )
        if frame.boot_epoch != view.boot_epoch:
            raise PaletteError(
                ErrorCode.STALE_FRAME, "the guest rebooted after this frame was captured"
            )
        if frame.controller_epoch != view.controller_epoch:
            raise PaletteError(
                ErrorCode.CONTROL_NOT_OWNED,
                "control of this VM changed hands after this frame was captured",
            )
        if frame.display_epoch != view.display_epoch:
            raise PaletteError(
                ErrorCode.STALE_FRAME, "the display changed after this frame was captured"
            )

    def _validate_frame_for_action(self, frame: Frame) -> CoordinateTransform:
        self._check_frame_authority(frame)
        if frame.action_seq != self.view.action_seq:
            raise PaletteError(
                ErrorCode.STALE_FRAME,
                "another action changed the desktop after this frame was captured",
                details={
                    "frame_action_seq": frame.action_seq,
                    "current_action_seq": self.view.action_seq,
                },
            )
        age = time.monotonic() - frame.monotonic_stamp
        if age > self.profile.frame_ttl_seconds:
            raise PaletteError(
                ErrorCode.STALE_FRAME,
                "this frame is older than the configured frame lifetime",
                details={
                    "age_seconds": round(age, 3),
                    "ttl_seconds": self.profile.frame_ttl_seconds,
                },
            )
        if self.view.native_size is not None and frame.native_size != self.view.native_size:
            raise PaletteError(
                ErrorCode.STALE_FRAME,
                "the framebuffer size changed since this frame was captured",
                details={"frame_size": frame.native_size.as_dict()},
            )
        return CoordinateTransform.from_frame(frame)

    def _requeue_or_stale(self, frame_id: str) -> Frame:
        """Re-read a frame after the size check.

        A resize retires every earlier frame, so the lookup itself now fails. Reporting
        that as ``FRAME_NOT_FOUND`` would be technically true but misleading — the agent
        needs to know the display changed and that it must observe again.
        """
        try:
            return self._frames.get(frame_id)
        except PaletteError as exc:
            if exc.code is ErrorCode.FRAME_NOT_FOUND:
                raise PaletteError(
                    ErrorCode.STALE_FRAME,
                    "the framebuffer changed since this frame was captured; observe again",
                    details={"frame_id": frame_id},
                ) from None
            raise

    async def _ensure_size_is_current(self) -> None:
        """Pre-action size check.

        QMP has no usable framebuffer-resize notification, so the only way to learn that the
        guest changed resolution is to look — and the design asks for that look *before* a
        coordinate action, because a click built on a frame from a surface that has since
        changed lands somewhere else.

        ``capture.size_check_interval_seconds`` trades that correctness for cost: a non-zero
        value lets a capture that recent stand as evidence of the current size, saving one
        screendump per action and opening a window in which a resize goes unnoticed. Zero,
        the default, means every coordinate action looks.
        """
        interval = self.profile.capture.size_check_interval_seconds
        if interval > 0 and (time.monotonic() - self._last_capture_at) < interval:
            return
        await self._capture_frame()

    # -- action planning -------------------------------------------------------------
    def _event_prefix(self) -> dict[str, Any]:
        # Note: the QMP ``device`` argument names the *display* device, not the keyboard
        # or pointer device id. Sending the keyboard's id here is a common and silent
        # mistake that routes input to the wrong console.
        return {
            "device": self.view.display_id,
            "head": self.view.head,
            "events": [],
        }

    async def _send_events(
        self,
        events: list[dict[str, Any]],
        *,
        timeout: float,
        on_sending: Callable[[], None] | None = None,
    ) -> None:
        arguments = self._event_prefix()
        arguments["events"] = events
        await self._qmp.execute(
            "input-send-event", arguments, timeout=timeout, on_sending=on_sending
        )

    async def operator_input(
        self, events: list[dict[str, Any]], validate: Callable[[], None]
    ) -> int:
        """Send one authenticated batch; successful held inputs survive the batch."""
        identity = self._identity()

        async def run() -> int:
            validate()
            self._require_identity(identity)
            self._ledger.note_presses(events)
            progress = _InputProgress(self.view, self.view.action_seq)
            await self._send_events(
                events,
                timeout=self.profile.timeouts.qmp_command_seconds,
                on_sending=progress.sending,
            )
            self._ledger.note_releases(events)
            self._require_identity(identity)
            if self._audit is not None:
                self._audit.record("operator.input", vm_id=self.view.vm_id, count=len(events))
            return self.view.action_seq

        return cast(
            int,
            await self.scheduler.submit(
                self.scheduler.make_job(JobKind.OPERATOR_INPUT, "operator", run)
            ),
        )

    async def _run_action(
        self, *, request_id: str, frame_id: str, action: Action, progress: _InputProgress
    ) -> tuple[Receipt, bytes | None]:
        started = time.monotonic()
        settle = self.profile.timeouts.settle_after_input_ms / 1000.0
        command_timeout = self.profile.timeouts.qmp_command_seconds

        try:
            frame = self._frames.get(frame_id)
            transform = self._validate_frame_for_action(frame)
            await self._ensure_size_is_current()
            transform = self._validate_frame_for_action(self._requeue_or_stale(frame_id))
            batches = self._planner.batches(action, transform)
        except PaletteError as exc:
            self._record_action("action.rejected", action, request_id, exc.code, 0)
            return _receipt(
                ok=False,
                request_id=request_id,
                action=action,
                effect_state=EffectState.NOT_SENT,
                action_seq=self.view.action_seq,
                error=exc,
                elapsed_ms=_elapsed_ms(started),
            ), None

        sent = 0
        pointer_moved = False
        try:
            for events, delay_ms in batches:
                # Presses are recorded *before* the send so that a failed press still ends
                # up in the cleanup ledger; releases are recorded only after the send
                # succeeds, because a release that did not go out leaves the key or button
                # exactly where it was.
                self._ledger.note_presses(events)
                await self._send_events(
                    events, timeout=command_timeout, on_sending=progress.sending
                )
                self._ledger.note_releases(events)
                sent += 1
                progress.accepted = sent
                progress.in_flight = False
                if any(event["type"] == "abs" for event in events):
                    pointer_moved = True
                if delay_ms:
                    await asyncio.sleep(delay_ms / 1000.0)
        except PaletteError as exc:
            if exc.code not in {ErrorCode.TIMEOUT, ErrorCode.QMP_UNAVAILABLE}:
                progress.in_flight = False
            if progress.attempted:
                exc = PaletteError(exc.code, exc.message, details=exc.details, retryable=False)
            self._record_action("action.failed", action, request_id, exc.code, sent)
            return _receipt(
                ok=False,
                request_id=request_id,
                action=action,
                effect_state=progress.effect,
                action_seq=progress.sequence,
                error=exc,
                elapsed_ms=_elapsed_ms(started),
                input_submitted=sent > 0,
                backend=self._backend_label(action),
            ), None

        # Every batch was acknowledged; a later observation failure cannot undo the input.
        progress.complete = True
        if settle:
            await asyncio.sleep(settle)

        cursor_source: CursorPositionSource = "last_command" if pointer_moved else "not_observed"
        png_after: bytes | None = None
        try:
            frame_after, captured_after = await self._capture_frame(
                cursor_position_source=cursor_source
            )
            self.view.screen_ready = True
            observation = ObservationState.CAPTURED
            error = None
            ok = True
            frame_out: Frame | None = frame_after
            png_after = captured_after.png_bytes
        except PaletteError as exc:
            self.view.screen_ready = False
            observation = ObservationState.FAILED
            ok = False
            frame_out = None
            error = PaletteError(
                ErrorCode.CAPTURE_FAILED_AFTER_INPUT,
                "the input was submitted but the follow-up observation failed",
                details={"capture_code": str(exc.code)},
                retryable=False,
            )
            logger.warning("post-action capture failed after submitted input: %s", exc.code)

        self._record_action(
            "action.completed" if ok else "action.observed_failed",
            action,
            request_id,
            error.code if error else None,
            sent,
        )
        return _receipt(
            ok=ok,
            request_id=request_id,
            action=action,
            effect_state=EffectState.SUBMITTED,
            action_seq=progress.sequence,
            frame=frame_out,
            error=error,
            elapsed_ms=_elapsed_ms(started),
            input_submitted=True,
            backend=self._backend_label(action),
            observation=observation,
            frame_observation_required=not ok,
        ), png_after

    def _backend_label(self, action: Action) -> str:
        if isinstance(action, (KeyAction, TypeTextAction)):
            return "qmp-keyboard"
        return "qmp-pointer"

    # -- release ---------------------------------------------------------------------
    async def _bounded_release(self) -> ReleaseResult:
        """Cleanup has its own total budget and is never owned by an MCP waiter."""
        try:
            async with asyncio.timeout(max(0.01, self.profile.timeouts.qmp_command_seconds)):
                return await self._release_all()
        except (TimeoutError, PaletteError):
            return ReleaseResult(
                ok=False,
                release_confirmed=False,
                note="input cleanup exceeded its budget or lost the management channel",
                released_keys=[],
                released_buttons=[],
            )

    async def _release_all(self) -> ReleaseResult:
        keys = list(self._ledger.keys)
        buttons = list(self._ledger.buttons)
        if not keys and not buttons:
            return ReleaseResult(ok=True, release_confirmed=True, note="nothing was held")

        confirmed = True
        note: str | None = None
        timeout = self.profile.timeouts.qmp_command_seconds
        for qcode in keys:
            try:
                await self._send_events([key_event(qcode, False)], timeout=timeout)
            except PaletteError as exc:
                confirmed = False
                note = f"a key release could not be confirmed: {exc.code}"
                break
            self._ledger.release_key(qcode)
        for button in buttons:
            try:
                await self._send_events([button_event(button, False)], timeout=timeout)
            except PaletteError as exc:
                confirmed = False
                note = f"a button release could not be confirmed: {exc.code}"
                break
            self._ledger.release_button(button)

        if self._audit:
            self._audit.record(
                "input.released",
                vm_id=self.view.vm_id,
                released_keys=len(keys),
                released_buttons=len(buttons),
                confirmed=confirmed,
            )
        return ReleaseResult(
            ok=confirmed,
            released_keys=keys,
            released_buttons=buttons,
            release_confirmed=confirmed,
            note=note,
        )

    # -- audit -----------------------------------------------------------------------
    def _record_action(
        self,
        event: str,
        action: Action,
        request_id: str,
        code: ErrorCode | None,
        batches: int,
    ) -> None:
        if not self._audit:
            return
        payload: dict[str, Any] = {
            "vm_id": self.view.vm_id,
            "instance_id": self.view.instance_id,
            "request_id": request_id,
            "action_type": action.type,
            "batches": batches,
            "action_seq": self.view.action_seq,
        }
        # Length, never content: the audit trail must not become a transcript.
        if isinstance(action, TypeTextAction):
            payload["text_length"] = len(action.text)
            payload["mode"] = action.mode
        elif isinstance(action, KeyAction):
            payload["key_count"] = len(action.keys)
        if code is not None:
            payload["error_code"] = str(code)
        self._audit.record(event, **payload)


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def _fingerprint(action_payload: dict[str, Any], frame_id: str | None) -> str:
    import hashlib
    import json

    canonical = json.dumps(
        {"action": action_payload, "frame_id": frame_id}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _receipt(**kwargs: Any) -> Receipt:
    """Build a receipt, translating the domain error into its structured payload."""
    action = kwargs.pop("action", None)
    if action is not None:
        kwargs["action_type"] = action.type
    error = kwargs.pop("error", None)
    payload = None
    if error is not None:
        payload = ErrorPayload(**error.as_payload())
    return Receipt(
        source=ActionSource.QMP,
        error=payload,
        **kwargs,
    )


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


__all__ = [
    "ActionController",
    "FrameRegistry",
    "InputLedger",
    "InstanceView",
    "RequestDeduplicator",
    "image_difference_ratio",
]
