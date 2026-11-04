"""Supervise the optional GIO display reader; QMP authority stays in the runtime."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import socket
import sys
import uuid
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .frame_source import FrameSource, read_bridge_packet
from .qmp.client import QmpClient

if TYPE_CHECKING:
    from .vm.process_guard import ProcessGuard

logger = logging.getLogger("palette.display.bridge")


class DisplayBridge:
    def __init__(self, helper: Path, instance_id: str) -> None:
        self.helper = helper
        self.source = FrameSource(instance_id)
        self.process: asyncio.subprocess.Process | None = None
        self.tasks: list[asyncio.Task[None]] = []
        self.stderr: deque[str] = deque(maxlen=12)
        self._ready = asyncio.get_running_loop().create_future()
        self._close_task: asyncio.Task[None] | None = None

    async def start(
        self, qmp: QmpClient, qemu_pid: int, head: int, guard: ProcessGuard | None
    ) -> None:
        from .vm.launcher import CREATION_FLAGS, build_child_env

        local, remote = socket.socketpair()
        name = f"display_{uuid.uuid4().hex}"
        registered = False
        try:
            arguments = [str(self.helper), "--qemu-pid", str(qemu_pid), "--head", str(head)]
            options: dict[str, Any] = {}
            if sys.platform != "win32":
                arguments += ["--fd", str(local.fileno())]
                options["pass_fds"] = (local.fileno(),)

            async def spawn() -> asyncio.subprocess.Process:
                self.process = await asyncio.create_subprocess_exec(
                    *arguments,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=build_child_env(),
                    cwd=self.helper.parent,
                    creationflags=CREATION_FLAGS,
                    **options,
                )
                return self.process

            spawning = asyncio.create_task(spawn())
            try:
                process = await asyncio.shield(spawning)
            except asyncio.CancelledError:
                from .cleanup import join_cleanup

                await join_cleanup(spawning)
                raise
            if sys.platform == "win32" and (guard is None or not guard.attach(process.pid)):
                raise OSError("display helper could not join the runtime Job Object")
            if sys.platform == "win32":
                if process.stdin is None:
                    raise OSError("display helper has no bootstrap pipe")
                process.stdin.write(base64.b64encode(local.share(process.pid)) + b"\n")
                await process.stdin.drain()
            self.tasks = [asyncio.create_task(self._read()), asyncio.create_task(self._stderr())]
            if sys.platform == "win32":
                await qmp.execute(
                    "get-win32-socket",
                    {"info": base64.b64encode(remote.share(qemu_pid)).decode(), "fdname": name},
                )
            else:
                await qmp.execute("getfd", {"fdname": name}, fd=remote.fileno())
            registered = True
            await qmp.execute("add_client", {"protocol": "@dbus-display", "fdname": name})
            registered = False  # QEMU consumed the named descriptor.
            async with asyncio.timeout(10):
                await asyncio.shield(self._ready)
        except BaseException:
            if registered:
                with contextlib.suppress(Exception):
                    await qmp.execute("closefd", {"fdname": name})
            await self.close()
            raise
        finally:
            local.close()
            remote.close()

    async def _read(self) -> None:
        if self.process is None or self.process.stdout is None:
            raise OSError("display helper has no output pipe")
        try:
            while True:
                header, data = await read_bridge_packet(self.process.stdout)
                if header.get("kind") == "ready":
                    if not self._ready.done():
                        self._ready.set_result(None)
                else:
                    self.source.apply(header, data)
        except (asyncio.IncompleteReadError, OSError, ValueError) as error:
            logger.warning("experimental display reader ended: %s: %s", type(error).__name__, error)
        finally:
            self.source.close()
            if not self._ready.done():
                self._ready.set_exception(OSError("display helper ended before registering"))
                self._ready.exception()
            if self._close_task is None:
                self._close_task = asyncio.create_task(self._close())

    async def _stderr(self) -> None:
        if self.process is None or self.process.stderr is None:
            raise OSError("display helper has no diagnostics pipe")
        while line := await self.process.stderr.readline():
            self.stderr.append(line.decode("utf-8", "replace").rstrip()[:1024])

    async def close(self) -> None:
        from .cleanup import join_cleanup

        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        await join_cleanup(self._close_task)

    async def _close(self) -> None:
        self.source.close()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        process = self.process
        if process is not None:
            if process.stdin is not None:
                process.stdin.close()  # Helper watches EOF even if the runtime is killed.
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()

            async def drain(reader: asyncio.StreamReader | None) -> None:
                if reader is not None:
                    while await reader.read(65536):
                        pass

            # A parser failure can leave a paused stdout pipe. Windows process.wait()
            # also waits for pipe teardown, so drain it even after killing the helper.
            async with asyncio.timeout(5):
                await asyncio.gather(process.wait(), drain(process.stdout), drain(process.stderr))


async def display_available(helper: str | None, binary: str, prefix: list[str]) -> bool:
    if not helper or not Path(helper).is_absolute() or not Path(helper).is_file():
        logger.warning("experimental display helper is missing; using VNC")
        return False
    return await dbus_available(binary, prefix)


async def dbus_available(binary: str, prefix: list[str]) -> bool:
    """Probe the selected build, without requiring or starting a frame helper."""
    from .errors import PaletteError
    from .vm.launcher import run_tool

    try:
        code, output, _ = await run_tool([binary, *prefix, "-display", "help"], timeout=5)
        return code == 0 and "dbus" in output.split()
    except (OSError, TimeoutError, PaletteError):
        logger.warning("D-Bus display probe failed; using VNC")
        return False
