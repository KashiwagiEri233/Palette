"""Private QMP transports.

Palette uses child stdio on POSIX and an instance-private AF_UNIX socket on Windows.
There is no TCP listener, no HMP, and no path by which an agent can reach the management channel:
the agent speaks MCP to the service, and only the service speaks QMP.

The transport is deliberately thin so that tests can substitute an in-process fake QEMU
without any code path difference from production.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from ..errors import QmpDisconnectedError

if TYPE_CHECKING:
    from ..vm.managed import ManagedProcess

logger = logging.getLogger("palette.qmp.transport")

#: QMP messages are newline-delimited JSON. ``query-qmp-schema`` returns a very large
#: single line, so the reader's buffer limit must be generous for a local trusted pipe.
DEFAULT_STREAM_LIMIT = 16 * 1024 * 1024


@runtime_checkable
class AsyncWritable(Protocol):
    def write(self, data: bytes) -> None: ...
    async def drain(self) -> None: ...
    def close(self) -> None: ...
    async def wait_closed(self) -> None: ...


class QmpTransport:
    """A QMP byte channel: a reader plus a writer, optionally backed by a process."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: AsyncWritable,
        *,
        process: ManagedProcess | None = None,
        name: str = "qmp",
        kind: str = "stream",
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.process = process
        self.name = name
        self.kind = kind
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    async def send_json(
        self, payload: dict[str, Any], *, on_sending: Callable[[], None] | None = None
    ) -> None:
        """Write one framed message, mapping pipe failures onto a Palette error."""
        if self._closed:
            raise QmpDisconnectedError("QMP transport is closed")
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
        try:
            if on_sending is not None:
                on_sending()
            self.writer.write(data)
            await self.writer.drain()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError) as exc:
            self._closed = True
            raise QmpDisconnectedError(f"QMP channel write failed: {type(exc).__name__}") from exc

    async def close(self) -> None:
        self._closed = True
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_writer(), name="qmp-transport-close")
        await asyncio.shield(self._close_task)

    async def _close_writer(self) -> None:
        try:
            self.writer.close()
        except Exception:
            return
        with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError):
            await self.writer.wait_closed()

    # -- process helpers -------------------------------------------------------------
    def terminate(self) -> None:
        """Ask the backing process to die. Only used by the VM manager's teardown."""
        if self.process is not None and self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.process.kill()

    @property
    def returncode(self) -> int | None:
        return None if self.process is None else self.process.returncode

    async def wait(self) -> int | None:
        if self.process is None:
            return None
        return await self.process.wait()


class ProcessQmpTransport(QmpTransport):
    """QMP over an already-started QEMU child process's stdin/stdout pipes."""

    def __init__(self, process: ManagedProcess, *, name: str = "qmp") -> None:
        if process.stdin is None or process.stdout is None:
            raise QmpDisconnectedError("QEMU was started without usable QMP pipes")
        super().__init__(process.stdout, process.stdin, process=process, name=name, kind="stdio")


async def connect_process_qmp(
    process: ManagedProcess, endpoint: str | None, *, timeout: float, name: str = "qmp"
) -> QmpTransport:
    if endpoint is None:
        return ProcessQmpTransport(process, name=name)
    from ..errors import ErrorCode, PaletteError
    from ..windows_unix import open_connection

    connecting = asyncio.create_task(open_connection(endpoint, limit=DEFAULT_STREAM_LIMIT))
    exited = asyncio.create_task(process.wait())
    handed_over = False
    try:
        async with asyncio.timeout(timeout):
            await asyncio.wait({connecting, exited}, return_when=asyncio.FIRST_COMPLETED)
            if connecting.done():
                reader, writer = connecting.result()
                handed_over = True
                return QmpTransport(reader, writer, process=process, name=name, kind="unix")
            raise QmpDisconnectedError(f"QEMU exited before QMP connection ({exited.result()})")
    except TimeoutError as exc:
        raise PaletteError(ErrorCode.QMP_UNAVAILABLE, "QMP socket connection timed out") from exc
    except OSError as exc:
        raise QmpDisconnectedError(f"QMP socket connection failed: {exc}") from exc
    finally:
        connecting.cancel()
        exited.cancel()
        await asyncio.gather(connecting, exited, return_exceptions=True)
        if not handed_over and not connecting.cancelled() and connecting.exception() is None:
            _reader, writer = connecting.result()
            writer.close()
            with contextlib.suppress(OSError, TimeoutError):
                async with asyncio.timeout(1):
                    await writer.wait_closed()


class MemoryTransport(QmpTransport):
    """In-process transport used by tests: a writer with a drain hook."""

    def __init__(self, reader: asyncio.StreamReader, sink: MemoryWriter) -> None:
        super().__init__(reader, sink, name="memory")
        self.sink = sink


class MemoryWriter:
    """Collects bytes written by the client and forwards them to a callback."""

    def __init__(self, on_write: Any) -> None:
        self._on_write = on_write
        self._closed = False

    def write(self, data: bytes) -> None:
        if self._closed:
            raise BrokenPipeError("closed")
        self._on_write(data)

    async def drain(self) -> None:
        await asyncio.sleep(0)

    def close(self) -> None:
        self._closed = True

    async def wait_closed(self) -> None:
        return None
