"""Process handles shared by direct Windows and supervised POSIX launches."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import time
from typing import Any, Protocol

import psutil

from ..errors import ErrorCode, PaletteError


async def finish_cleanup[T](task: asyncio.Task[T]) -> T:
    """Join owned cleanup even if the caller is cancelled more than once."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


def terminate_process_group(pid: int, created: float | None = None, timeout: float = 5.0) -> bool:
    """Kill and confirm owned descendants, including a group whose leader already exited."""
    known: dict[int, psutil.Process] = {}
    try:
        leader = psutil.Process(pid)
        if created is not None and leader.create_time() != created:
            return False
        known.update(
            (process.pid, process) for process in [leader, *leader.children(recursive=True)]
        )
    except psutil.NoSuchProcess:
        pass
    except psutil.Error:
        return False
    getpgid = getattr(os, "getpgid", None)
    killpg = getattr(os, "killpg", None)
    groups = {pid}
    if getpgid is not None:
        for process in known.values():
            with contextlib.suppress(OSError):
                if getpgid(process.pid) == process.pid:
                    groups.add(process.pid)
    deadline = time.monotonic() + timeout
    while True:
        if getpgid is not None:
            for process in psutil.process_iter():
                with contextlib.suppress(OSError, psutil.Error):
                    if getpgid(process.pid) in groups:
                        known[process.pid] = process
        alive = []
        for process in known.values():
            with contextlib.suppress(psutil.Error):
                if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                    alive.append(process)
        if not alive:
            return True
        if killpg is not None:
            for group in groups:
                with contextlib.suppress(OSError):
                    killpg(group, getattr(signal, "SIGKILL", 9))
        for process in alive:
            with contextlib.suppress(psutil.Error):
                process.kill()
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


async def wait_process_exit(process: asyncio.subprocess.Process, timeout: float = 5.0) -> int:
    """Wait for the process itself, independently of unread subprocess stream buffers."""
    async with asyncio.timeout(timeout):
        while process.returncode is None:  # noqa: ASYNC110 - OS child watcher, not an application event
            await asyncio.sleep(0.01)
    return process.returncode


class ManagedProcess(Protocol):
    pid: int
    stdin: asyncio.StreamWriter | None
    stdout: asyncio.StreamReader | None
    stderr: asyncio.StreamReader | None

    @property
    def returncode(self) -> int | None: ...
    async def wait(self) -> int: ...
    def kill(self) -> None: ...


class SupervisedProcess:
    supervised = True

    def __init__(
        self,
        supervisor: asyncio.subprocess.Process,
        status: asyncio.StreamReader,
        status_transport: asyncio.ReadTransport,
        liveness_fd: int,
    ) -> None:
        self._supervisor = supervisor
        self._status = status
        self._status_transport = status_transport
        self._liveness_fd: int | None = liveness_fd
        os.set_blocking(liveness_fd, False)
        self.stdin = supervisor.stdin
        self.stdout = supervisor.stdout
        self.stderr = supervisor.stderr
        self.pid = 0
        self._created = 0.0
        self._returncode: int | None = None
        self._ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._watcher = asyncio.create_task(self._watch(), name="qemu-supervisor-status")
        self.exit_reason: str | None = None

    @property
    def description(self) -> str:
        return "POSIX supervisor (liveness pipe, process group, sampled RSS)"

    def attach(self, pid: int) -> bool:
        return self.pid == pid and self._supervisor.returncode is None

    @property
    def returncode(self) -> int | None:
        return self._returncode

    async def ready(self) -> None:
        await asyncio.shield(self._ready)

    def close(self) -> None:
        if self._liveness_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._liveness_fd)
            self._liveness_fd = None

    def kill(self) -> None:
        if self._returncode is not None:
            return
        if self._liveness_fd is not None:
            try:
                os.write(self._liveness_fd, b"K")
                return
            except OSError:
                self.close()
        self._kill_group()

    def _kill_group(self) -> None:
        killpg = getattr(os, "killpg", None)
        if self.pid and killpg is not None:
            with contextlib.suppress(psutil.Error, OSError):
                process = psutil.Process(self.pid)
                if process.create_time() == self._created:
                    killpg(self.pid, getattr(signal, "SIGKILL", 9))

    async def wait(self) -> int:
        await asyncio.shield(self._watcher)
        if self._returncode is None:
            raise PaletteError(ErrorCode.TIMEOUT, "QEMU exit could not be confirmed")
        return self._returncode

    async def _watch(self) -> None:
        final_code: int | None = None
        try:
            while line := await self._status.readline():
                message: Any = json.loads(line)
                if "pid" in message:
                    self.pid = int(message["pid"])
                    self._created = float(message["created"])
                    if not self._ready.done():
                        self._ready.set_result(None)
                if "error" in message:
                    raise PaletteError(ErrorCode.STARTUP_FAILED, str(message["error"]))
                if "returncode" in message:
                    final_code = int(message["returncode"])
                    self.exit_reason = message.get("reason")
        except Exception as exc:
            if not self._ready.done():
                self._ready.set_exception(exc)
                self._ready.exception()
        finally:
            self.close()
            self._status_transport.close()
            if final_code is None and await asyncio.to_thread(
                terminate_process_group,
                self.pid or self._supervisor.pid,
                self._created if self.pid else None,
            ):
                final_code = -getattr(signal, "SIGKILL", 9)
            if self._supervisor.returncode is None and final_code is None:
                with contextlib.suppress(ProcessLookupError):
                    self._supervisor.kill()
            try:
                await wait_process_exit(self._supervisor)
            except TimeoutError:
                self._supervisor.kill()
                await wait_process_exit(self._supervisor)
            self._returncode = final_code
            if not self._ready.done():
                self._ready.set_exception(
                    PaletteError(ErrorCode.STARTUP_FAILED, "supervisor exited before QEMU started")
                )
                self._ready.exception()
