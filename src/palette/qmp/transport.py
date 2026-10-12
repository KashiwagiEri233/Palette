"""Private QMP transports.

Palette talks to QEMU over pipes attached to a child process it started itself. There is
no TCP listener, no HMP, and no path by which an agent can reach the management channel:
the agent speaks MCP to the service, and only the service speaks QMP.

The transport is deliberately thin so that tests can substitute an in-process fake QEMU
without any code path difference from production.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any, Protocol, runtime_checkable

from ..errors import QmpDisconnectedError

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
        process: asyncio.subprocess.Process | None = None,
        name: str = "qmp",
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.process = process
        self.name = name
        self._closed = False

    async def send_json(self, payload: dict[str, Any]) -> None:
        """Write one framed message, mapping pipe failures onto a Palette error."""
        if self._closed:
            raise QmpDisconnectedError("QMP transport is closed")
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
        try:
            self.writer.write(data)
            await self.writer.drain()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError) as exc:
            self._closed = True
            raise QmpDisconnectedError(f"QMP channel write failed: {type(exc).__name__}") from exc

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
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

    def __init__(self, process: asyncio.subprocess.Process, *, name: str = "qmp") -> None:
        if process.stdin is None or process.stdout is None:
            raise QmpDisconnectedError("QEMU was started without usable QMP pipes")
        super().__init__(process.stdout, process.stdin, process=process, name=name)


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
