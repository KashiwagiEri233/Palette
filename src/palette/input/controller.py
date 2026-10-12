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
import io
import logging
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, cast

from PIL import Image

from ..audit import AuditLog
from ..capture.base import CaptureBackend, CapturedImage
from ..config import Profile
from ..errors import ErrorCode, PaletteError
from ..models import (
    Action,
    ActionSource,
    ClickAction,
    DragAction,
    EffectState,
    ErrorPayload,
    Frame,
    KeyAction,
    MoveAction,
    ObservationState,
    Receipt,
    ReleaseResult,
    ScrollAction,
    Size,
    TypeTextAction,
    Viewport,
    WaitReason,
    WaitResult,
    parse_action,
)
from ..qmp.client import QmpClient
from .coordinates import CoordinateTransform, interpolate, transform_id_for
from .keyboard import plan_chord, plan_text

logger = logging.getLogger("palette.input")

#: A job may wait this long for the queue before the service reports BUSY.
DEFAULT_QUEUE_WAIT_SECONDS = 5.0

#: Pixel-difference heuristic settings for ``vm_wait``.
_STABLE_SAMPLE_INTERVAL = 0.25
_THUMBNAIL_SIZE = (64, 64)


#: How the service describes where the pointer is. It never claims to have observed the
#: real cursor: a QMP screendump does not include the hardware cursor.
CursorPositionSource = Literal["not_observed", "last_command"]


class JobKind(StrEnum):
    ACTION = "action"
    OBSERVE = "observe"
    RELEASE = "release"


@dataclass(slots=True)
class InstanceView:
    """The mutable identity of one QEMU process, shared by the manager and controller.

    Kept as a plain object so that both sides mutate the same epochs; anything derived
    from an epoch is therefore invalidated by construction rather than by remembering to
    call an invalidation helper in the right order.
    """

    vm_id: str
    instance_id: str
    profile_id: str
    display_id: str
    head: int
    boot_epoch: int = 1
    display_epoch: int = 1
    controller_epoch: int = 1
    action_seq: int = 0
    native_size: Size | None = None
    state: str = "STOPPED"
    qmp_ready: bool = False
    screen_ready: bool = False
    desktop_ready: bool | None = None
    started_at: datetime | None = None
    last_error: ErrorPayload | None = None


# --------------------------------------------------------------------------------------
# supporting structures
# --------------------------------------------------------------------------------------


class InputLedger:
    """What the service believes is currently held down.

    A press that we failed to release is remembered deliberately: the plan requires the
    cleanup path to know about a possibly-stuck key or button, and requires the service
    to admit when it cannot confirm a release rather than claiming success.
    """

    __slots__ = ("buttons", "keys")

    def __init__(self) -> None:
        self.keys: list[str] = []
        self.buttons: list[str] = []

    def press_key(self, qcode: str) -> None:
        if qcode not in self.keys:
            self.keys.append(qcode)

    def release_key(self, qcode: str) -> None:
        with contextlib.suppress(ValueError):
            self.keys.remove(qcode)

    def press_button(self, button: str) -> None:
        if button not in self.buttons:
            self.buttons.append(button)

    def release_button(self, button: str) -> None:
        with contextlib.suppress(ValueError):
            self.buttons.remove(button)

    @property
    def is_clean(self) -> bool:
        return not self.keys and not self.buttons

    def snapshot(self) -> dict[str, list[str]]:
        return {"keys": list(self.keys), "buttons": list(self.buttons)}


class FrameRegistry:
    """A small, bounded store of frames the agent may still act on.

    Retired identifiers are remembered briefly and with a reason. That distinction
    matters to the agent: "the display changed while you were looking at that frame" is a
    different situation, with a different recovery, from "that identifier was never
    issued". Both are refusals, but only one of them means "observe again and retry".
    """

    def __init__(self, *, max_entries: int = 16, max_retired: int = 32) -> None:
        self._frames: OrderedDict[str, Frame] = OrderedDict()
        #: One small grayscale thumbnail per frame. Frames themselves are never retained,
        #: but "has the screen changed since the picture you saw?" needs *something* to
        #: compare against, and a 64x64 thumbnail is a few kilobytes rather than a
        #: megabyte of someone's desktop.
        self._thumbnails: OrderedDict[str, bytes] = OrderedDict()
        self._retired: OrderedDict[str, str] = OrderedDict()
        self._max_entries = max_entries
        self._max_retired = max_retired

    def add(self, frame: Frame, thumbnail: bytes | None = None) -> Frame:
        self._frames[frame.frame_id] = frame
        if thumbnail is not None:
            self._thumbnails[frame.frame_id] = thumbnail
        while len(self._frames) > self._max_entries:
            evicted, _ = self._frames.popitem(last=False)
            self._thumbnails.pop(evicted, None)
        return frame

    def thumbnail(self, frame_id: str) -> bytes | None:
        return self._thumbnails.get(frame_id)

    def get(self, frame_id: str) -> Frame:
        frame = self._frames.get(frame_id)
        if frame is not None:
            return frame
        reason = self._retired.get(frame_id)
        if reason is not None:
            raise PaletteError(
                ErrorCode.STALE_FRAME,
                f"this frame was retired because {reason}; observe again",
                details={"frame_id": frame_id, "reason": reason},
            )
        raise PaletteError(
            ErrorCode.FRAME_NOT_FOUND,
            "that frame is not known to this instance; observe again before acting",
            details={"frame_id": frame_id},
        )

    def latest(self) -> Frame | None:
        if not self._frames:
            return None
        return list(self._frames.values())[-1]

    def clear(self, *, reason: str = "the display changed") -> None:
        for frame_id in self._frames:
            self._retired[frame_id] = reason
        self._frames.clear()
        self._thumbnails.clear()
        while len(self._retired) > self._max_retired:
            self._retired.popitem(last=False)


class RequestDeduplicator:
    """``request_id`` is an application-level idempotency key, not a QMP or JSON-RPC id.

    A replay with identical parameters returns the original receipt; a replay with
    different parameters is a conflict. Only in-process, bounded memory is promised —
    exactly-once across a process restart is out of scope, and no log can undo input that
    already reached the guest.
    """

    def __init__(self, *, max_entries: int = 256) -> None:
        self._records: OrderedDict[str, tuple[str, Receipt]] = OrderedDict()
        self._inflight: dict[str, tuple[str, asyncio.Future[Receipt]]] = {}
        self._max_entries = max_entries

    def lookup(self, request_id: str, fingerprint: str) -> Receipt | None:
        record = self._records.get(request_id)
        if record is None:
            return None
        stored_fingerprint, receipt = record
        if stored_fingerprint != fingerprint:
            raise PaletteError(
                ErrorCode.REQUEST_ID_CONFLICT,
                "this request_id was already used with different parameters",
                details={"request_id": request_id},
            )
        return receipt.model_copy(update={"deduplicated": True})

    def begin(self, request_id: str, fingerprint: str) -> asyncio.Future[Receipt] | None:
        """Return an existing in-flight future for an identical concurrent request."""
        existing = self._inflight.get(request_id)
        if existing is None:
            return None
        stored_fingerprint, future = existing
        if stored_fingerprint != fingerprint:
            raise PaletteError(
                ErrorCode.REQUEST_ID_CONFLICT,
                "this request_id is already running with different parameters",
                details={"request_id": request_id},
            )
        return future

    def attach(self, request_id: str, fingerprint: str, future: asyncio.Future[Receipt]) -> None:
        self._inflight[request_id] = (fingerprint, future)

    def complete(self, request_id: str, fingerprint: str, receipt: Receipt) -> None:
        self._inflight.pop(request_id, None)
        self._records[request_id] = (fingerprint, receipt)
        while len(self._records) > self._max_entries:
            self._records.popitem(last=False)

    def abandon(self, request_id: str) -> None:
        self._inflight.pop(request_id, None)


@dataclass(slots=True)
class _Job:
    kind: JobKind
    label: str
    runner: Callable[[], Coroutine[Any, Any, Any]]
    future: asyncio.Future[Any]
    started: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[Any] | None = None
    cancelled: bool = False


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
        self._jobs: deque[_Job] = deque()
        self._wake = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None
        self._current: _Job | None = None
        self._closing = False
        self._last_capture_at: float = 0.0

    # -- lifecycle -------------------------------------------------------------------
    async def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(
                self._run_worker(), name=f"actions-{self.view.vm_id}"
            )

    async def shutdown(self, *, release: bool = True) -> ReleaseResult | None:
        """Stop accepting work, drop queued jobs, and try to leave the guest unheld.

        The release runs before ``_closing`` blocks new submissions, because refusing to
        submit is not a reason to leave a mouse button pressed inside the guest.
        """
        result: ReleaseResult | None = None
        if release:
            try:
                result = await self._release_all()
            except PaletteError as exc:
                result = ReleaseResult(
                    ok=False,
                    release_confirmed=False,
                    note=f"release before shutdown failed: {exc.code}",
                    released_keys=list(self._ledger.keys),
                    released_buttons=list(self._ledger.buttons),
                )

        self._closing = True
        for job in list(self._jobs):
            job.cancelled = True
            if not job.future.done():
                job.future.set_exception(
                    PaletteError(ErrorCode.CANCELLED, "the VM is shutting down")
                )
                with contextlib.suppress(BaseException):
                    job.future.exception()
        self._jobs.clear()
        self._wake.set()

        if self._current is not None and self._current.task is not None:
            self._current.task.cancel()
            with contextlib.suppress(BaseException):
                await self._current.task
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._worker
            self._worker = None
        self._frames.clear(reason="the VM is shutting down")
        return result

    # -- public API ------------------------------------------------------------------
    async def observe(
        self,
        *,
        request_id: str | None = None,
        max_long_edge: int | None = None,
    ) -> tuple[Frame, CapturedImage]:
        """Take a fresh capture, queued so it cannot observe a half-finished action."""
        job = self._make_job(
            JobKind.OBSERVE,
            "observe",
            lambda: self._capture_frame(max_long_edge=max_long_edge),
        )
        return cast("tuple[Frame, CapturedImage]", await self._submit(job))

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

        cached = self._dedupe.lookup(request_id, fingerprint)
        if cached is not None:
            if self._audit:
                self._audit.record(
                    "action.deduplicated",
                    vm_id=self.view.vm_id,
                    request_id=request_id,
                    action_type=action.type,
                )
            return cached, None
        inflight = self._dedupe.begin(request_id, fingerprint)
        if inflight is not None:
            return cast("tuple[Receipt, bytes | None]", await asyncio.shield(inflight))

        job = self._make_job(
            JobKind.ACTION,
            f"action:{action.type}",
            lambda: self._run_action(request_id=request_id, frame_id=frame_id, action=action),
        )
        self._dedupe.attach(request_id, fingerprint, job.future)
        try:
            receipt, png = cast("tuple[Receipt, bytes | None]", await self._submit(job))
        except BaseException:
            self._dedupe.abandon(request_id)
            raise
        self._dedupe.complete(request_id, fingerprint, receipt)
        return receipt, png

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
            _frame, captured_baseline = await self._capture_frame()
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
                ), current
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
                    ), current
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
        runner: Callable[[], Awaitable[Any]],
    ) -> tuple[Any | None, Frame | None, bytes | None, PaletteError | None]:
        """Run a guest-side operation inside the same single-writer slot.

        The plan requires guest text input and app launching to share the per-VM scheduler,
        control ownership, de-duplication and audit with QMP clicks. Routing them through
        the same job queue is what makes that true: a guest launch and a click can never
        interleave halfway through each other.

        A guest operation that changes the desktop advances ``action_seq`` exactly like a
        QMP action, so frames captured before it are fenced by the ordinary mechanism
        rather than by a special case.
        """

        async def _job() -> tuple[Any | None, Frame | None, bytes | None, PaletteError | None]:
            try:
                result = await runner()
            except PaletteError as exc:
                self._record_guest(label, None, exc.code)
                return None, None, None, exc
            self.view.action_seq += 1
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
            self._record_guest(label, frame is not None, None)
            return result, frame, png, None

        job = self._make_job(JobKind.ACTION, label, _job)
        return cast(
            "tuple[Any | None, Frame | None, bytes | None, PaletteError | None]",
            await self._submit(job),
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

    async def release_inputs(self) -> ReleaseResult:
        """Release every key and button the service believes it holds."""
        job = self._make_job(JobKind.RELEASE, "release", self._release_all)
        with contextlib.suppress(PaletteError):
            return cast(
                ReleaseResult, await self._submit(job, queue_wait=DEFAULT_QUEUE_WAIT_SECONDS)
            )
        # A release that could not even be queued still has to report the truth.
        return ReleaseResult(
            ok=False,
            release_confirmed=False,
            note="release could not be scheduled; the input ledger is unchanged",
            released_keys=list(self._ledger.keys),
            released_buttons=list(self._ledger.buttons),
        )

    async def emergency_release(self) -> ReleaseResult:
        """Cancel whatever is running, then release. Used by stop/reset and by the CLI.

        The cleanup runs as its own task so that cancelling the action cannot cancel the
        very coroutine responsible for letting go of the mouse button.
        """
        if self._current is not None and self._current.task is not None:
            self._current.cancelled = True
            self._current.task.cancel()
        task = asyncio.create_task(self._release_all(), name=f"emergency-release-{self.view.vm_id}")
        try:
            return await task
        except asyncio.CancelledError:
            # Keep waiting for the release: it is the point of this method.
            with contextlib.suppress(Exception):
                await asyncio.shield(task)
            raise
        except PaletteError as exc:
            return ReleaseResult(
                ok=False,
                release_confirmed=False,
                note=f"release failed: {exc.code}",
                released_keys=list(self._ledger.keys),
                released_buttons=list(self._ledger.buttons),
            )

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
        return len(self._jobs) + (1 if self._current is not None else 0)

    @property
    def latest_frame(self) -> Frame | None:
        return self._frames.latest()

    @property
    def frame_ttl_seconds(self) -> float:
        return self.profile.frame_ttl_seconds

    # -- scheduler -------------------------------------------------------------------
    def _make_job(
        self, kind: JobKind, label: str, runner: Callable[[], Coroutine[Any, Any, Any]]
    ) -> _Job:
        return _Job(
            kind=kind,
            label=label,
            runner=runner,
            future=asyncio.get_running_loop().create_future(),
        )

    async def _submit(self, job: _Job, *, queue_wait: float | None = None) -> Any:
        """Queue one job and wait for its result.

        The return type is deliberately ``Any``: each job kind resolves to a different
        value, and every call site casts to the type its own runner produces. Widening it
        to a union would make three unrelated result shapes look interchangeable.
        """
        if self._closing:
            raise PaletteError(ErrorCode.VM_NOT_RUNNING, "this VM instance is shutting down")
        if len(self._jobs) >= self.profile.resources.max_queue_depth:
            raise PaletteError(
                ErrorCode.BUSY,
                "the action queue for this VM is full",
                details={"queue_depth": len(self._jobs)},
            )
        self._jobs.append(job)
        self._wake.set()

        wait_limit = (
            queue_wait if queue_wait is not None else self.profile.resources.max_queued_wait_seconds
        )
        try:
            await asyncio.wait_for(job.started.wait(), timeout=wait_limit)
        except TimeoutError:
            if not job.started.is_set():
                job.cancelled = True
                with contextlib.suppress(ValueError):
                    self._jobs.remove(job)
                raise PaletteError(
                    ErrorCode.BUSY,
                    "the VM stayed busy longer than this request is allowed to queue",
                    details={"queued_seconds": wait_limit},
                ) from None

        try:
            return await asyncio.wait_for(
                job.future, timeout=self.profile.resources.max_action_seconds
            )
        except TimeoutError:
            if job.task is not None:
                job.cancelled = True
                job.task.cancel()
            # An observation or a release that timed out has no ambiguous side effect;
            # an action does, and saying so is the difference between a safe retry and a
            # duplicated click.
            code = (
                ErrorCode.ACTION_OUTCOME_UNKNOWN
                if job.kind is JobKind.ACTION
                else ErrorCode.TIMEOUT
            )
            message = (
                "the action did not finish in time and may already have taken effect"
                if job.kind is JobKind.ACTION
                else f"{job.label} did not finish in time"
            )
            raise PaletteError(
                code,
                message,
                details={"timeout_seconds": self.profile.resources.max_action_seconds},
            ) from None
        except asyncio.CancelledError:
            if job.task is not None:
                job.cancelled = True
                job.task.cancel()
            raise

    async def _run_worker(self) -> None:
        while not self._closing:
            if not self._jobs:
                self._wake.clear()
                try:
                    await self._wake.wait()
                except asyncio.CancelledError:
                    raise
                continue
            job = self._jobs.popleft()
            if job.cancelled or job.future.done():
                continue
            self._current = job
            job.started.set()
            task = asyncio.create_task(job.runner(), name=f"job-{job.label}")
            job.task = task
            try:
                result = await task
            except asyncio.CancelledError:
                # Two cases: the caller cancelled this specific job, or the worker is
                # being shut down. In the first case keep serving; in the second, stop.
                if self._closing:
                    raise
                if not job.future.done():
                    job.future.set_exception(
                        PaletteError(
                            ErrorCode.CANCELLED,
                            "the action was cancelled before it completed",
                        )
                    )
                    with contextlib.suppress(BaseException):
                        job.future.exception()
            except PaletteError as exc:
                if not job.future.done():
                    job.future.set_exception(exc)
                    with contextlib.suppress(BaseException):
                        job.future.exception()
            except Exception as exc:
                logger.exception("action job %s failed", job.label)
                if not job.future.done():
                    job.future.set_exception(
                        PaletteError(
                            ErrorCode.INTERNAL_ERROR,
                            f"internal error while running {job.label}: {type(exc).__name__}",
                        )
                    )
                    with contextlib.suppress(BaseException):
                        job.future.exception()
            else:
                if not job.future.done():
                    job.future.set_result(result)
            finally:
                self._current = None
                job.task = None

    # -- capture ---------------------------------------------------------------------
    async def _capture_frame(
        self,
        *,
        max_long_edge: int | None = None,
        cursor_position_source: CursorPositionSource = "not_observed",
    ) -> tuple[Frame, CapturedImage]:
        sink = self._frame_path()
        captured = await self._capture.capture(
            display_id=self.view.display_id, head=self.view.head, sink=sink
        )
        if max_long_edge is not None and max_long_edge != self.profile.capture.max_long_edge:
            captured = _rescale(captured, max_long_edge, self.profile.capture.max_bytes)
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

    async def _send_events(self, events: list[dict[str, Any]], *, timeout: float) -> None:
        arguments = self._event_prefix()
        arguments["events"] = events
        await self._qmp.execute("input-send-event", arguments, timeout=timeout)

    async def _run_action(
        self, *, request_id: str, frame_id: str, action: Action
    ) -> tuple[Receipt, bytes | None]:
        started = time.monotonic()
        settle = self.profile.timeouts.settle_after_input_ms / 1000.0
        command_timeout = self.profile.timeouts.qmp_command_seconds

        try:
            frame = self._frames.get(frame_id)
            transform = self._validate_frame_for_action(frame)
            await self._ensure_size_is_current()
            transform = self._validate_frame_for_action(self._requeue_or_stale(frame_id))
            batches = self._plan_batches(action, transform)
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
                self._note_presses(events)
                await self._send_events(events, timeout=command_timeout)
                self._note_releases(events)
                sent += 1
                if any(event["type"] == "abs" for event in events):
                    pointer_moved = True
                if delay_ms:
                    await asyncio.sleep(delay_ms / 1000.0)
        except PaletteError as exc:
            state = EffectState.NOT_SENT if sent == 0 else EffectState.PARTIAL
            if exc.code is ErrorCode.TIMEOUT or exc.code is ErrorCode.QMP_UNAVAILABLE:
                state = EffectState.UNKNOWN if sent else EffectState.NOT_SENT
            self._record_action("action.failed", action, request_id, exc.code, sent)
            return _receipt(
                ok=False,
                request_id=request_id,
                action=action,
                effect_state=state,
                action_seq=self.view.action_seq,
                error=exc,
                elapsed_ms=_elapsed_ms(started),
                input_submitted=sent > 0,
                backend=self._backend_label(action),
            ), None
        except asyncio.CancelledError:
            await self._cleanup_after_cancel(request_id, action, sent)
            raise

        # The input is out. From here on the transaction is committed: the action
        # sequence advances and every earlier frame is fenced, whether or not the
        # post-action capture succeeds.
        self.view.action_seq += 1
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
            action_seq=self.view.action_seq,
            frame=frame_out,
            error=error,
            elapsed_ms=_elapsed_ms(started),
            input_submitted=True,
            backend=self._backend_label(action),
            observation=observation,
            frame_observation_required=not ok,
        ), png_after

    async def _cleanup_after_cancel(self, request_id: str, action: Action, sent: int) -> None:
        """A cancelled action still has to let go of anything it pressed."""
        logger.info("action %s cancelled after %d batch(es); releasing inputs", request_id, sent)
        release = asyncio.create_task(self._release_all(), name="cancel-release")
        try:
            await release
        except Exception:
            logger.warning("release after cancellation failed", exc_info=True)
        self._record_action("action.cancelled", action, request_id, ErrorCode.CANCELLED, sent)

    def _note_presses(self, events: list[dict[str, Any]]) -> None:
        """Record a press before it is sent.

        Under-reporting a held key is worse than over-reporting one: the cleanup path only
        releases what it knows about, so an optimistically recorded press is safe and a
        missed one is not.
        """
        for event in events:
            data = event.get("data") or {}
            if event["type"] == "key":
                qcode = (data.get("key") or {}).get("data")
                if isinstance(qcode, str) and data.get("down"):
                    self._ledger.press_key(qcode)
            elif event["type"] == "btn":
                button = data.get("button")
                if isinstance(button, str) and not button.startswith("wheel") and data.get("down"):
                    self._ledger.press_button(button)

    def _note_releases(self, events: list[dict[str, Any]]) -> None:
        """Record a release only once the send came back successfully."""
        for event in events:
            data = event.get("data") or {}
            if event["type"] == "key":
                qcode = (data.get("key") or {}).get("data")
                if isinstance(qcode, str) and not data.get("down"):
                    self._ledger.release_key(qcode)
            elif event["type"] == "btn":
                button = data.get("button")
                if (
                    isinstance(button, str)
                    and not button.startswith("wheel")
                    and not data.get("down")
                ):
                    self._ledger.release_button(button)

    def _backend_label(self, action: Action) -> str:
        if isinstance(action, (KeyAction, TypeTextAction)):
            return "qmp-keyboard"
        return "qmp-pointer"

    def _plan_batches(
        self, action: Action, transform: CoordinateTransform
    ) -> list[tuple[list[dict[str, Any]], int]]:
        """Turn an action into ordered ``input-send-event`` batches.

        One phase per message: a press and its release are never merged into a single
        request, because HID polling, event synchronisation and guest scheduling all sit
        between them and a merged message makes the timing unobservable.
        """
        batches: list[tuple[list[dict[str, Any]], int]] = []

        if isinstance(action, MoveAction):
            batches.append((_abs_events(*transform.to_qemu(action.x, action.y)), 0))
        elif isinstance(action, ClickAction):
            batches.extend(self._click_batches(action, transform))
        elif isinstance(action, ScrollAction):
            batches.append((_abs_events(*transform.to_qemu(action.x, action.y)), 0))
            button = "wheel-up" if action.direction == "up" else "wheel-down"
            for _ in range(action.ticks):
                batches.append(([_btn_event(button, True)], 8))
                batches.append(([_btn_event(button, False)], 8))
        elif isinstance(action, DragAction):
            batches.extend(self._drag_batches(action, transform))
        elif isinstance(action, KeyAction):
            plan = plan_chord(action.keys, hold_ms=action.hold_ms)
            batches.extend(_key_plan_batches(plan, hold_ms=action.hold_ms))
        elif isinstance(action, TypeTextAction):
            plan = plan_text(action.text, max_chars=self.profile.text.max_chars)
            interval = self.profile.text.char_interval_ms
            batches.extend(_key_plan_batches(plan, hold_ms=interval, gap_ms=interval))
            budget_ms = self.profile.text.max_total_ms
            if sum(delay for _events, delay in batches) > budget_ms:
                raise PaletteError(
                    ErrorCode.RATE_LIMITED,
                    "this text would exceed the configured per-action typing budget",
                    details={"budget_ms": budget_ms},
                )
        else:  # pragma: no cover - the action union is closed
            raise PaletteError(ErrorCode.UNSUPPORTED_CAPABILITY, "unsupported action type")
        return batches

    def _click_batches(
        self, action: ClickAction, transform: CoordinateTransform
    ) -> list[tuple[list[dict[str, Any]], int]]:
        timeouts = self.profile.timeouts
        batches: list[tuple[list[dict[str, Any]], int]] = [
            (_abs_events(*transform.to_qemu(action.x, action.y)), 0)
        ]
        for index in range(action.count):
            batches.append(([_btn_event(action.button, True)], timeouts.click_hold_ms))
            batches.append(([_btn_event(action.button, False)], 0))
            if index < action.count - 1:
                batches.append(([], timeouts.double_click_gap_ms))
        return [batch for batch in batches if batch[0] or batch[1]]

    def _drag_batches(
        self, action: DragAction, transform: CoordinateTransform
    ) -> list[tuple[list[dict[str, Any]], int]]:
        # More samples for longer paths, but bounded by the schema's 64 point limit.
        steps = max(2, min(6, 48 // max(1, len(action.points) - 1)))
        path = interpolate([(point.x, point.y) for point in action.points], steps_per_segment=steps)
        per_step_ms = int(action.duration_ms / max(1, len(path) - 1)) if action.duration_ms else 0
        batches: list[tuple[list[dict[str, Any]], int]] = [
            (_abs_events(*transform.to_qemu(*path[0])), 0),
            ([_btn_event(action.button, True)], per_step_ms),
        ]
        for point in path[1:]:
            batches.append((_abs_events(*transform.to_qemu(*point)), per_step_ms))
        batches.append(([_btn_event(action.button, False)], 0))
        return batches

    # -- release ---------------------------------------------------------------------
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
                await self._send_events([_key_event(qcode, False)], timeout=timeout)
            except PaletteError as exc:
                confirmed = False
                note = f"a key release could not be confirmed: {exc.code}"
                break
            self._ledger.release_key(qcode)
        for button in buttons:
            try:
                await self._send_events([_btn_event(button, False)], timeout=timeout)
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


def _abs_events(qx: int, qy: int) -> list[dict[str, Any]]:
    # X and Y travel together so the pointer never appears at an intermediate corner.
    return [
        {"type": "abs", "data": {"axis": "x", "value": qx}},
        {"type": "abs", "data": {"axis": "y", "value": qy}},
    ]


def _btn_event(button: str, down: bool) -> dict[str, Any]:
    return {"type": "btn", "data": {"button": button, "down": down}}


def _key_event(qcode: str, down: bool) -> dict[str, Any]:
    return {"type": "key", "data": {"key": {"type": "qcode", "data": qcode}, "down": down}}


def _key_plan_batches(
    plan: Any, *, hold_ms: int, gap_ms: int = 0
) -> list[tuple[list[dict[str, Any]], int]]:
    """One message per key event, with a hold between down and up.

    Modifier release order is preserved from the plan (which already reverses it), so the
    wiring here stays a straightforward translation.
    """
    batches: list[tuple[list[dict[str, Any]], int]] = []
    events = list(plan.events)
    for index, event in enumerate(events):
        delay = 0
        if event.down:
            next_down_is_other = index + 1 < len(events) and events[index + 1].down
            delay = 0 if next_down_is_other else hold_ms
        else:
            delay = gap_ms
        batches.append(([_key_event(event.qcode, event.down)], delay))
    return batches


def _fingerprint(action_payload: dict[str, Any], frame_id: str) -> str:
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


def _rescale(captured: CapturedImage, max_long_edge: int, max_bytes: int) -> CapturedImage:
    """Apply a caller's image-size preference without hiding the transform change."""
    with Image.open(io.BytesIO(captured.png_bytes)) as image:
        longest = max(image.width, image.height)
        if longest <= max_long_edge:
            return captured
        ratio = max_long_edge / longest
        width = max(2, int(image.width * ratio))
        height = max(2, int(image.height * ratio))
        resized = image.convert("RGB").resize((width, height), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        resized.save(buffer, format="PNG", optimize=True)
    payload = buffer.getvalue()
    if len(payload) > max_bytes:
        raise PaletteError(
            ErrorCode.CAPTURE_TOO_LARGE,
            "the resized image still exceeds the configured byte limit",
        )
    return CapturedImage(
        png_bytes=payload,
        native_size=captured.native_size,
        image_size=Size(width=width, height=height),
        format_used=captured.format_used,
        scaled=True,
        duration_ms=captured.duration_ms,
        source_path=captured.source_path,
    )


def image_thumbnail(png_bytes: bytes, *, size: tuple[int, int] = _THUMBNAIL_SIZE) -> bytes:
    """A small grayscale reduction of a frame, used only for change detection."""
    try:
        with Image.open(io.BytesIO(png_bytes)) as image:
            return image.convert("L").resize(size).tobytes()
    except Exception:
        return b""


def thumbnail_difference(left: bytes, right: bytes) -> float:
    """Mean absolute difference in 0..1 between two thumbnails."""
    if not left or not right or len(left) != len(right):
        return 1.0
    total = sum(abs(a - b) for a, b in zip(left, right, strict=True))
    return total / (255 * len(left))


def image_difference_ratio(
    left: bytes, right: bytes, *, size: tuple[int, int] = _THUMBNAIL_SIZE
) -> float:
    """Mean absolute pixel difference in 0..1 between two PNGs.

    Downscaled to a thumbnail first so that the heuristic is cheap and insensitive to
    single-pixel noise. It is a heuristic: the plan is explicit that pixel stability is
    not proof that an application is idle.
    """
    try:
        with Image.open(io.BytesIO(left)) as a, Image.open(io.BytesIO(right)) as b:
            if a.size != b.size:
                return 1.0
            left_thumb = a.convert("L").resize(size)
            right_thumb = b.convert("L").resize(size)
            left_bytes = left_thumb.tobytes()
            right_bytes = right_thumb.tobytes()
    except Exception:
        return 1.0
    return thumbnail_difference(left_bytes, right_bytes)


__all__ = [
    "ActionController",
    "FrameRegistry",
    "InputLedger",
    "InstanceView",
    "RequestDeduplicator",
    "image_difference_ratio",
]
