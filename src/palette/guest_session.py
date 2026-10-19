"""Read-only readiness heartbeat from the unprivileged X11 resize helper."""

from __future__ import annotations

import asyncio
import json
import time

from .guest_channel import GuestChannel


class GuestSession:
    def __init__(self, endpoint: str) -> None:
        self.channel = GuestChannel(endpoint)
        self.last_seen = 0.0

    @property
    def online(self) -> bool:
        return self.last_seen > 0 and time.monotonic() - self.last_seen < 8

    async def run(self) -> None:
        buffer = bytearray()
        while True:
            try:
                await self.channel.open()
                buffer.extend(await self.channel.read(4))
                if len(buffer) > 4096:
                    raise ValueError("session heartbeat too large")
                while b"\n" in buffer:
                    line, _, tail = buffer.partition(b"\n")
                    buffer = bytearray(tail)
                    value = json.loads(line)
                    if value == {"version": 1, "session": "x11", "resize": True}:
                        self.last_seen = time.monotonic()
            except TimeoutError:
                continue
            except (OSError, EOFError, ConnectionError, ValueError, RecursionError):
                self.last_seen = 0
                buffer.clear()
                await self.channel.close()
                await asyncio.sleep(1)

    async def close(self) -> None:
        self.last_seen = 0
        await self.channel.close()
