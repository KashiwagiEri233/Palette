"""One bounded, cancellable observation/input queue per VM instance."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..config import Profile
from ..errors import ErrorCode, PaletteError

logger = logging.getLogger("palette.input")


class JobKind(StrEnum):
    ACTION = "action"
    OPERATOR_INPUT = "operator_input"
    OBSERVE = "observe"
    RELEASE = "release"


@dataclass(slots=True)
class Job:
    kind: JobKind
    label: str
    runner: Callable[[], Coroutine[Any, Any, Any]]
    future: asyncio.Future[Any]
    started: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[Any] | None = None
    cancelled: bool = False
    abort_reason: PaletteError | None = None
    abort_result: Callable[[PaletteError], Any] | None = None
    completed: Callable[[Any], None] | None = None


class ActionScheduler:
    def __init__(
        self,
        profile: Profile,
        *,
        guard: Callable[[], None],
        release: Callable[[], Awaitable[object]],
        is_clean: Callable[[], bool],
    ) -> None:
        self.profile = profile
        self.mutation_guard = guard
        self._bounded_release = release
        self.is_clean = is_clean
        self.jobs: deque[Job] = deque()
        self.wake = asyncio.Event()
        self.worker: asyncio.Task[None] | None = None
        self.current: Job | None = None
        self.closing = False

    async def start(self, vm_id: str) -> None:
        if self.worker is None:
            self.worker = asyncio.create_task(self._run_worker(), name=f"actions-{vm_id}")

    async def close(self) -> None:
        self.closing = True
        self.cancel_all(PaletteError(ErrorCode.VM_NOT_RUNNING, "this VM instance is shutting down"))
        if self.worker is not None:
            self.worker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.worker
            self.worker = None

    def cancel_all(self, error: PaletteError) -> None:
        for job in list(self.jobs):
            self.cancel_job(job, error)
        if self.current is not None:
            self.cancel_job(self.current, error)
        self.wake.set()

    @property
    def queued_jobs(self) -> int:
        return len(self.jobs) + (1 if self.current is not None else 0)

    def _finish_job(
        self, job: Job, *, result: Any = None, error: PaletteError | None = None
    ) -> None:
        if job.future.done():
            return
        if error is not None and job.abort_result is not None:
            result = job.abort_result(error)
            error = None
        if error is not None:
            job.future.set_exception(error)
            job.future.exception()
        else:
            if job.completed is not None:
                job.completed(result)
            job.future.set_result(result)

    def cancel_job(self, job: Job, error: PaletteError) -> None:
        if job.cancelled or job.future.done():
            return
        job.cancelled = True
        job.abort_reason = error
        if job.task is not None:
            job.task.cancel()
        else:
            with contextlib.suppress(ValueError):
                self.jobs.remove(job)
            job.started.set()
            self._finish_job(job, error=error)

    def make_job(
        self, kind: JobKind, label: str, runner: Callable[[], Coroutine[Any, Any, Any]]
    ) -> Job:
        return Job(
            kind=kind,
            label=label,
            runner=runner,
            future=asyncio.get_running_loop().create_future(),
        )

    async def submit(self, job: Job, *, queue_wait: float | None = None) -> Any:
        """Queue one job and wait for its result.

        The return type is deliberately ``Any``: each job kind resolves to a different
        value, and every call site casts to the type its own runner produces. Widening it
        to a union would make three unrelated result shapes look interchangeable.
        """
        if self.closing:
            raise PaletteError(ErrorCode.VM_NOT_RUNNING, "this VM instance is shutting down")
        if len(self.jobs) >= self.profile.resources.max_queue_depth:
            raise PaletteError(
                ErrorCode.BUSY,
                "the action queue for this VM is full",
                details={"queue_depth": len(self.jobs)},
            )
        self.jobs.append(job)
        self.wake.set()

        wait_limit = (
            queue_wait if queue_wait is not None else self.profile.resources.max_queued_wait_seconds
        )
        try:
            await asyncio.wait_for(job.started.wait(), timeout=wait_limit)
        except TimeoutError:
            if not job.started.is_set():
                error = PaletteError(
                    ErrorCode.BUSY,
                    "the VM stayed busy longer than this request is allowed to queue",
                    details={"queued_seconds": wait_limit},
                )
                self.cancel_job(job, error)
        except asyncio.CancelledError:
            self.cancel_job(job, PaletteError(ErrorCode.CANCELLED, "the request was cancelled"))
            raise
        try:
            return await asyncio.shield(job.future)
        except asyncio.CancelledError:
            self.cancel_job(job, PaletteError(ErrorCode.CANCELLED, "the request was cancelled"))
            raise

    async def _run_worker(self) -> None:
        while not self.closing:
            if not self.jobs:
                self.wake.clear()
                try:
                    await self.wake.wait()
                except asyncio.CancelledError:
                    raise
                continue
            job = self.jobs.popleft()
            if job.cancelled or job.future.done():
                continue
            self.current = job
            job.started.set()
            guard_passed = False

            async def guarded_runner(current: Job = job) -> Any:
                nonlocal guard_passed
                if current.kind is JobKind.ACTION and self.mutation_guard is not None:
                    self.mutation_guard()
                guard_passed = True
                return await current.runner()

            task = asyncio.create_task(guarded_runner(), name=f"job-{job.label}")
            job.task = task
            timer = asyncio.get_running_loop().call_later(
                self.profile.resources.max_action_seconds,
                self.cancel_job,
                job,
                PaletteError(
                    ErrorCode.ACTION_OUTCOME_UNKNOWN
                    if job.kind in {JobKind.ACTION, JobKind.OPERATOR_INPUT}
                    else ErrorCode.TIMEOUT,
                    f"{job.label} exceeded its execution deadline",
                    retryable=False,
                ),
            )
            result: Any = None
            error: PaletteError | None = None
            try:
                result = await task
            except asyncio.CancelledError:
                error = job.abort_reason or PaletteError(
                    ErrorCode.CANCELLED, "the action was cancelled"
                )
            except PaletteError as exc:
                error = exc
            except Exception as exc:
                logger.exception("action job %s failed", job.label)
                error = PaletteError(
                    ErrorCode.INTERNAL_ERROR,
                    f"internal error while running {job.label}: {type(exc).__name__}",
                )
            finally:
                timer.cancel()
                if (
                    (job.kind is JobKind.ACTION and guard_passed)
                    or (job.kind is JobKind.OPERATOR_INPUT and error is not None)
                ) and not self.is_clean():
                    await self._bounded_release()
                self._finish_job(job, result=result, error=error)
                self.current = None
                job.task = None
