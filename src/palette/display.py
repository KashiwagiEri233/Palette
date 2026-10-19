"""Ticketed, protocol-enforced read-only RFB gateway for local desktop clients."""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import struct
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

from Crypto.Cipher import DES
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from websockets.typing import Origin

from .errors import ErrorCode, PaletteError

if TYPE_CHECKING:
    from .service import PaletteService

VERSION = b"RFB 003.008\n"
DISPLAY_ENCODINGS = frozenset(
    {
        0,
        1,
        2,
        5,
        6,
        7,
        16,
        21,
        -260,
        -223,
        -224,
        -239,
        -307,
        -308,
        -312,
        -313,
        *range(-32, -22),
        *range(-256, -246),
    }
)


def vnc_response(password: str, challenge: bytes) -> bytes:
    key = password.encode("ascii")[:8].ljust(8, b"\0")
    key = bytes(int(f"{byte:08b}"[::-1], 2) for byte in key)
    return DES.new(key, DES.MODE_ECB).encrypt(challenge)  # noqa: S304 - VNCAuth requires DES on this private loopback channel


class RfbFilter:
    """Incremental parser: WebSocket packet boundaries are not RFB boundaries."""

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.encodings: set[int] = set()

    def feed(self, data: bytes) -> bytes:
        self.buffer.extend(data)
        if len(self.buffer) > 256 * 1024:
            raise ValueError("RFB client buffer exceeded its limit")
        output = bytearray()
        while self.buffer:
            kind = self.buffer[0]
            if kind not in {0, 2, 3, 150, 248}:
                raise ValueError("RFB client attempted a non-display operation")
            if kind == 2:
                if len(self.buffer) < 4:
                    break
                count = struct.unpack_from(">H", self.buffer, 2)[0]
                if count > 256:
                    raise ValueError("too many RFB encodings")
                size = 4 + count * 4
            elif kind == 248:
                if len(self.buffer) < 9:
                    break
                length = self.buffer[8]
                if length > 64 or -312 not in self.encodings:
                    raise ValueError("invalid RFB fence")
                size = 9 + length
            else:
                size = {0: 20, 3: 10, 150: 10}[kind]
            if len(self.buffer) < size:
                break
            message = bytes(self.buffer[:size])
            del self.buffer[:size]
            if kind == 2:
                values = struct.unpack(f">{count}i", message[4:])
                selected = [value for value in values if value in DISPLAY_ENCODINGS]
                if 0 not in selected:
                    selected.append(0)
                self.encodings = set(selected)
                message = (
                    b"\x02\0"
                    + struct.pack(">H", len(selected))
                    + struct.pack(f">{len(selected)}i", *selected)
                )
            elif kind == 0:
                bpp, depth, endian, truecolor = message[4:8]
                if (
                    bpp not in {8, 16, 32}
                    or not 0 < depth <= bpp
                    or endian not in {0, 1}
                    or truecolor != 1
                ):
                    raise ValueError("invalid RFB pixel format")
            elif kind == 150 and (-313 not in self.encodings or message[1] not in {0, 1}):
                raise ValueError("continuous updates were not negotiated")
            output.extend(message)
        return bytes(output)


class WsReader:
    def __init__(self, ws: ServerConnection) -> None:
        self.ws = ws
        self.buffer = bytearray()

    async def read(self, count: int) -> bytes:
        async with asyncio.timeout(5):
            while len(self.buffer) < count:
                message = await self.ws.recv()
                if not isinstance(message, bytes):
                    raise ValueError("binary RFB stream required")
                self.buffer.extend(message)
                if len(self.buffer) > 256 * 1024:
                    raise ValueError("RFB handshake buffer exceeded its limit")
        result = bytes(self.buffer[:count])
        del self.buffer[:count]
        return result


@dataclass
class Ticket:
    vm_id: str
    instance_id: str
    expires: float


class DisplayGateway:
    def __init__(self, service: PaletteService) -> None:
        self.service = service
        self.server: Server | None = None
        self.port = 0
        self.tickets: dict[str, Ticket] = {}

    async def start(self) -> None:
        self.server = await serve(
            self._handle,
            "127.0.0.1",
            0,
            origins=[
                Origin("app://palette"),
                Origin("http://127.0.0.1:5173"),
                Origin("http://localhost:5173"),
            ],
            max_size=64 * 1024,
            max_queue=4,
            write_limit=64 * 1024,
            compression=None,
            open_timeout=5,
            close_timeout=2,
        )
        self.port = int(self.server.sockets[0].getsockname()[1])

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        self.tickets.clear()

    def ticket(self, vm_id: str, instance_id: str) -> dict[str, str]:
        instance = self.service.manager.get(vm_id)
        self.service.require_instance_id(instance, instance_id)
        if instance.console_address is None:
            raise PaletteError(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                "restart this VM with the desktop service to enable its console",
            )
        self.tickets = {
            key: ticket for key, ticket in self.tickets.items() if ticket.expires > time.monotonic()
        }
        if len(self.tickets) >= 64:
            raise PaletteError(ErrorCode.BUSY, "too many pending display connections")
        token = secrets.token_urlsafe(32)
        self.tickets[token] = Ticket(vm_id, instance_id, time.monotonic() + 60)
        return {"url": f"ws://127.0.0.1:{self.port}/display?ticket={token}"}

    async def _handle(self, ws: ServerConnection) -> None:
        writer: asyncio.StreamWriter | None = None
        tasks: list[asyncio.Task[None]] = []
        ticket: Ticket | None = None
        accepted = False
        try:
            if ws.request is None:
                return
            url = urlsplit(ws.request.path)
            token = parse_qs(url.query).get("ticket", [""])[0]
            ticket = self.tickets.pop(token, None)
            if url.path != "/display" or ticket is None or ticket.expires <= time.monotonic():
                await ws.close(code=1008, reason="display ticket is invalid")
                return
            instance = self.service.manager.get(ticket.vm_id)
            self.service.require_instance_id(instance, ticket.instance_id)
            if instance.console_address is None or instance.console_password is None:
                return
            accepted = True
            async with asyncio.timeout(5):
                reader, writer = await asyncio.open_connection(*instance.console_address)
                if await reader.readexactly(12) != VERSION:
                    raise ValueError("RFB 3.8 is required")
                writer.write(VERSION)
                count = (await reader.readexactly(1))[0]
                if not 0 < count <= 32 or 2 not in await reader.readexactly(count):
                    raise ValueError("VNC authentication is required")
                writer.write(b"\x02")
                writer.write(vnc_response(instance.console_password, await reader.readexactly(16)))
                await writer.drain()
                if await reader.readexactly(4) != b"\0" * 4:
                    raise ValueError("VNC authentication failed")
                writer.write(b"\x01")
                await writer.drain()
                header = await reader.readexactly(24)
                width, height = struct.unpack_from(">HH", header)
                name_size = struct.unpack_from(">I", header, 20)[0]
                if width * height > 8192 * 4320 or width < 2 or height < 2 or name_size > 4096:
                    raise ValueError("invalid RFB server geometry")
                server_init = header + await reader.readexactly(name_size)
                downstream = WsReader(ws)
                await ws.send(VERSION)
                if await downstream.read(12) != VERSION:
                    raise ValueError("RFB 3.8 is required")
                await ws.send(b"\x01\x01")
                if await downstream.read(1) != b"\x01":
                    raise ValueError("unexpected RFB security choice")
                await ws.send(b"\0" * 4)
                if await downstream.read(1) not in {b"\0", b"\x01"}:
                    raise ValueError("invalid RFB client init")
                await ws.send(server_init)

            async def upstream() -> None:
                while True:
                    data = await reader.read(64 * 1024)
                    if not data:
                        return
                    # RFB updates are incremental; never discard bytes under backpressure.
                    async with asyncio.timeout(5):
                        await ws.send(data)

            async def downstream_messages() -> None:
                parser = RfbFilter()
                pending = bytes(downstream.buffer)
                while True:
                    if not pending:
                        if parser.buffer:
                            async with asyncio.timeout(5):
                                pending_message = await ws.recv()
                        else:
                            pending_message = await ws.recv()
                        if not isinstance(pending_message, bytes):
                            raise ValueError("binary RFB required")
                        pending = pending_message
                    data = parser.feed(pending)
                    pending = b""
                    if data:
                        writer.write(data)
                        async with asyncio.timeout(5):
                            await writer.drain()

            tasks = [asyncio.create_task(upstream()), asyncio.create_task(downstream_messages())]
            completed, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in completed:
                task.result()
        except (
            ValueError,
            OSError,
            PaletteError,
            TimeoutError,
            ConnectionClosed,
            asyncio.IncompleteReadError,
        ):
            with contextlib.suppress(ConnectionClosed):
                await ws.close(code=1008, reason="display connection ended")
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if writer is not None:
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()
            if accepted and ticket is not None:
                with contextlib.suppress(PaletteError):
                    current = self.service.manager.find(ticket.vm_id)
                    if current is not None and current.view.instance_id == ticket.instance_id:
                        await self.service.control.revoke(
                            ticket.vm_id, expected_instance_id=ticket.instance_id
                        )
