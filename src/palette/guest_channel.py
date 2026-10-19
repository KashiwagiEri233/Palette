"""Private QEMU chardev streams. Windows pipes are connected before QMP startup."""

from __future__ import annotations

import asyncio
import contextlib
import sys
from typing import Any


class GuestChannel:
    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.pipe: Any = None
        self.buffer = bytearray()

    @property
    def connected(self) -> bool:
        return self.pipe is not None or self.writer is not None

    async def open(self) -> None:
        if self.connected:
            return
        if sys.platform == "win32":
            from .operator.windows import connect

            self.pipe = await connect(self.endpoint)
        else:
            self.reader, self.writer = await asyncio.open_unix_connection(self.endpoint)

    async def read(self, timeout: float = 3.0) -> bytes:
        if self.pipe is not None:
            # Kernel deadline avoids cancelling overlapped I/O and destroying the pipe
            # when the guest service has not booted yet.
            worker = asyncio.create_task(self.pipe._io("read", 65536, timeout))
            try:
                return bytes(await asyncio.shield(worker))
            except asyncio.CancelledError:
                # Preserve the duplex pipe for resynchronization after a cancelled RPC.
                # The overlapped read has its own kernel timeout.
                with contextlib.suppress(OSError, TimeoutError):
                    await worker
                raise
        if self.reader is None:
            raise ConnectionError("guest channel is closed")
        async with asyncio.timeout(timeout):
            data = await self.reader.read(65536)
        if not data:
            raise EOFError("guest channel disconnected")
        return data

    async def readexactly(self, size: int) -> bytes:
        while len(self.buffer) < size:
            try:
                self.buffer.extend(await self.read(timeout=1))
            except TimeoutError:
                continue
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result

    async def write(self, data: bytes) -> None:
        if self.pipe is not None:
            await self.pipe.write(data)
        elif self.writer is not None:
            self.writer.write(data)
            await self.writer.drain()
        else:
            raise ConnectionError("guest channel is closed")

    async def close(self) -> None:
        pipe, self.pipe = self.pipe, None
        writer, self.writer = self.writer, None
        self.reader = None
        self.buffer.clear()
        if pipe is not None:
            await pipe.close()
        if writer is not None:
            writer.close()
            with contextlib.suppress(ConnectionError, OSError, TimeoutError):
                async with asyncio.timeout(1):
                    await writer.wait_closed()
