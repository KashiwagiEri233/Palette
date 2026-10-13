"""Asyncio stream adapter for a private Unix domain socket."""

from __future__ import annotations

import asyncio
import contextlib


class UnixChannel:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer

    async def readexactly(self, size: int) -> bytes:
        return await self.reader.readexactly(size)

    async def write(self, data: bytes) -> None:
        self.writer.write(data)
        await self.writer.drain()

    async def close(self) -> None:
        self.writer.close()
        with contextlib.suppress(ConnectionError, OSError, TimeoutError):
            async with asyncio.timeout(1.0):
                await self.writer.wait_closed()
