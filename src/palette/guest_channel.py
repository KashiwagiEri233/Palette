"""Instance-private socket streams for optional QEMU guest services."""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time
from typing import Any

CHANNEL_ATTACH_TIMEOUT = 30.0


class GuestChannel:
    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.buffer = bytearray()
        self._open_lock = asyncio.Lock()
        self._generation = 0
        self._state = "pending"
        self._started: float | None = None
        self._elapsed: float | None = None
        self._error: str | None = None
        self._error_detail: str | None = None

    @property
    def connected(self) -> bool:
        return (
            self.writer is not None
            and not self.writer.is_closing()
            and self.reader is not None
            and not self.reader.at_eof()
            and self.reader.exception() is None
        )

    def status(self) -> dict[str, Any]:
        elapsed = self._elapsed
        if self._state == "connecting" and self._started is not None:
            elapsed = time.monotonic() - self._started
        return {
            "transport": "unix",
            "state": (
                "connected"
                if self.connected
                else "closed"
                if self._state == "connected"
                else self._state
            ),
            "connected": self.connected,
            "elapsed_seconds": elapsed,
            "error": self._error,
            "error_detail": self._error_detail,
        }

    async def open(self, *, timeout: float = CHANNEL_ATTACH_TIMEOUT) -> None:
        budget = asyncio.timeout(timeout)
        async with budget, self._open_lock:
            if self.connected:
                return
            await self.close()
            generation = self._generation
            self._state = "connecting"
            self._started = time.monotonic()
            self._elapsed = None
            self._error = None
            self._error_detail = None
            try:
                if sys.platform == "win32":
                    from .windows_unix import open_connection

                    reader, writer = await open_connection(self.endpoint)
                else:
                    reader, writer = await asyncio.open_unix_connection(self.endpoint)
                if generation != self._generation:
                    writer.close()
                    await writer.wait_closed()
                    raise ConnectionError("guest channel was closed during connection")
                self.reader, self.writer = reader, writer
                self._state = "connected"
            except BaseException as exc:
                if budget.expired():
                    self._state = "failed"
                    self._error = "TimeoutError"
                    self._error_detail = "guest socket attachment exceeded its deadline"
                else:
                    self._state = (
                        "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
                    )
                    self._error = type(exc).__name__
                    self._error_detail = str(exc)[:1024]
                raise
            finally:
                self._elapsed = time.monotonic() - self._started

    async def read(self, timeout: float = 3.0) -> bytes:
        if self.reader is None:
            raise ConnectionError("guest channel is closed")
        async with asyncio.timeout(timeout):
            data = await self.reader.read(65536)
        if not data:
            self._state = "closed"
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
        if self.writer is not None:
            self.writer.write(data)
            await self.writer.drain()
        else:
            raise ConnectionError("guest channel is closed")

    async def close(self) -> None:
        self._generation += 1
        writer, self.writer = self.writer, None
        self.reader = None
        self.buffer.clear()
        self._state = "closed"
        if writer is not None:
            writer.close()
            with contextlib.suppress(ConnectionError, OSError, TimeoutError):
                async with asyncio.timeout(1):
                    await writer.wait_closed()
