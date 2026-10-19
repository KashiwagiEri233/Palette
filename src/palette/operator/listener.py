"""Local listener and connection tasks, shared by operator and agent protocols."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..cleanup import close_all, join_cleanup
from .paths import IS_WINDOWS, RuntimePaths
from .protocol import MAX_CLIENTS, Channel, Endpoint
from .unix import UnixChannel

if TYPE_CHECKING:
    from .windows import WindowsListener
logger = logging.getLogger("palette.operator")


class LocalListener:
    def __init__(
        self,
        paths: RuntimePaths,
        endpoint: Endpoint,
        handle: Callable[[Channel], Coroutine[Any, Any, None]],
    ) -> None:
        self.paths = paths
        self.endpoint = endpoint
        self.handle = handle
        self._unix: asyncio.AbstractServer | None = None
        self._pipe: WindowsListener | None = None
        self._accept_task: asyncio.Task[None] | None = None
        self.handlers: set[asyncio.Task[None]] = set()
        self._closing = False
        self._socket_identity: tuple[int, int] | None = None
        self._close_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.paths.prepare()
        try:
            if IS_WINDOWS:
                from .windows import WindowsListener

                self._pipe = WindowsListener(self.endpoint.address)
                self._accept_task = asyncio.create_task(
                    self._accept_pipes(), name="operator-accept"
                )
            else:
                self.paths.remove_stale_socket(self.endpoint.generation)
                start_unix: Any = getattr(asyncio, "start_unix_server", None)
                self._unix = await start_unix(
                    self._accept_unix,
                    path=self.endpoint.address,
                    limit=256 * 1024,
                    backlog=MAX_CLIENTS,
                )
                path = Path(self.endpoint.address)
                os.chmod(path, 0o600)
                info = path.stat()
                self._socket_identity = (info.st_dev, info.st_ino)
            self.paths.publish(self.endpoint)
        except BaseException:
            try:
                await self.close()
            except BaseException as exc:
                logger.error("listener startup cleanup failed: %s", type(exc).__name__)
            raise

    def _accept_unix(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._schedule(UnixChannel(reader, writer))

    async def _accept_pipes(self) -> None:
        if self._pipe is None:
            return
        while True:
            if self._closing:
                return
            try:
                channel = await self._pipe.accept()
            except OSError:
                logger.info("operator pipe accept stopped")
                return
            self._schedule(channel)

    def _schedule(self, channel: Channel) -> None:
        if self._closing or len(self.handlers) >= MAX_CLIENTS:
            task = asyncio.create_task(channel.close(), name="operator-reject")
        else:
            task = asyncio.create_task(self.handle(channel), name="operator-client")
        self.handlers.add(task)
        task.add_done_callback(self.handlers.discard)

    async def close(self) -> None:
        self._closing = True
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close(), name="ipc-listener-close")
        await join_cleanup(self._close_task)

    async def _close(self) -> None:
        await close_all(
            (
                self._retire,
                self._close_unix,
                self._close_pipe,
                self._close_acceptor,
                self._close_handlers,
                self._unlink,
            )
        )

    async def _retire(self) -> None:
        self.paths.retire(self.endpoint.generation)

    async def _close_unix(self) -> None:
        if self._unix is not None:
            self._unix.close()
            await self._unix.wait_closed()
            self._unix = None

    async def _close_pipe(self) -> None:
        if self._pipe is not None:
            await self._pipe.close()
            self._pipe = None

    async def _close_acceptor(self) -> None:
        if self._accept_task is not None:
            self._accept_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._accept_task
            self._accept_task = None

    async def _close_handlers(self) -> None:
        tasks = set(self.handlers)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _unlink(self) -> None:
        self._remove_socket()

    def _remove_socket(self) -> None:
        if self._socket_identity is None:
            return
        path = Path(self.endpoint.address)
        with contextlib.suppress(FileNotFoundError):
            info = path.lstat()
            if (info.st_dev, info.st_ino) == self._socket_identity:
                path.unlink()
        self._socket_identity = None
