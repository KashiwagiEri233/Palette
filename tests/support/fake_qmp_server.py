"""An in-process QMP server, used to test framing, correlation and failure handling.

The client's interesting behaviour lives in its reader: interleaved events, responses that
arrive out of order, a peer that disappears mid-command, a single enormous line. None of
that is practical to provoke with the subprocess fake, so this server scripts it directly
over a real socket with real framing.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from contextlib import asynccontextmanager
from typing import Any

from palette.qmp.transport import QmpTransport

#: Matches production: ``query-qmp-schema`` really is a single multi-hundred-kilobyte line.
DEFAULT_LIMIT = 16 * 1024 * 1024

GREETING = {
    "QMP": {
        "version": {"qemu": {"major": 11, "minor": 1, "micro": 2}, "package": "v11.1.2"},
        "capabilities": ["oob"],
    }
}


class FakeQmpServer:
    """Scripted QMP peer."""

    def __init__(
        self,
        *,
        greeting: bool = True,
        delayed_greeting: float = 0.0,
        error_commands: dict[str, tuple[str, str]] | None = None,
        response_delay: float = 0.0,
        slow_commands: set[str] | None = None,
        events: list[dict[str, Any]] | None = None,
        event_after_commands: int | None = None,
        schema_bytes: int = 0,
        schema_advertises_png: bool = True,
        disconnect_after: int | None = None,
        split_writes: bool = False,
        limit: int = DEFAULT_LIMIT,
    ) -> None:
        self.greeting_enabled = greeting
        self.delayed_greeting = delayed_greeting
        self.error_commands = error_commands or {}
        self.response_delay = response_delay
        #: Commands answered after ``response_delay`` instead of immediately. Used to make
        #: two in-flight commands come back in the opposite order.
        self.slow_commands = slow_commands or set()
        self.events = events or []
        self.event_after_commands = event_after_commands
        self.schema_bytes = schema_bytes
        self.schema_advertises_png = schema_advertises_png
        self.disconnect_after = disconnect_after
        self.split_writes = split_writes
        self.limit = limit
        self.received: list[dict[str, Any]] = []
        self.commands: list[dict[str, Any]] = []
        self.connections = 0

    # -- protocol --------------------------------------------------------------------
    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        try:
            if self.delayed_greeting:
                await asyncio.sleep(self.delayed_greeting)
            if self.greeting_enabled:
                await self._send(writer, GREETING)

            while True:
                line = await reader.readline()
                if not line:
                    return
                text = line.strip()
                if not text:
                    continue
                try:
                    message = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if not isinstance(message, dict):
                    # The client must survive a peer that sends something that is not a
                    # QMP message; the double must not crash on it either.
                    self.received.append({"malformed": text.decode("utf-8", "replace")})
                    continue
                self.received.append(message)
                if "execute" in message:
                    self.commands.append(message)

                if self.disconnect_after is not None and len(self.received) > self.disconnect_after:
                    writer.close()
                    return

                await self._respond(writer, message)
        except (ConnectionResetError, ConnectionAbortedError, asyncio.CancelledError):
            return
        except Exception:
            raise
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    async def _respond(self, writer: asyncio.StreamWriter, message: dict[str, Any]) -> None:
        command = message.get("execute")
        message_id = message.get("id")
        if command is None:
            return
        if self.response_delay or command in self.slow_commands:
            await asyncio.sleep(self.response_delay or 0.2)

        if command in self.error_commands:
            error_class, description = self.error_commands[command]
            await self._send(
                writer,
                {"error": {"class": error_class, "desc": description}, "id": message_id},
            )
            return

        result: Any = {}
        if command == "query-version":
            result = {"qemu": {"major": 11, "minor": 1, "micro": 2}, "package": "v11.1.2"}
        elif command == "query-commands":
            result = [{"name": name} for name in ("screendump", "input-send-event", "query-status")]
        elif command == "query-qmp-schema":
            arguments = [
                {"name": "filename", "type": "str"},
                {"name": "device", "type": "str"},
                {"name": "head", "type": "int"},
            ]
            if self.schema_advertises_png:
                arguments.append({"name": "format", "type": "ImageFormat"})
            result = [
                {
                    "name": "screendump",
                    "meta-type": "command",
                    "arg-type": arguments,
                    "note": "x" * max(0, self.schema_bytes),
                }
            ]
        elif command == "query-status":
            result = {"status": "running", "running": True}

        await self._send(writer, {"return": result, "id": message_id})

        if (
            self.event_after_commands is not None
            and len(self.commands) >= self.event_after_commands
        ):
            for event in self.events:
                await self._send(writer, event)

    async def _send(self, writer: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, separators=(",", ":")).encode() + b"\n"
        if self.split_writes and len(data) > 8:
            # A peer is allowed to split a frame across TCP segments; the reader must
            # reassemble it rather than assume one line per read.
            midpoint = len(data) // 2
            for chunk in (data[:midpoint], data[midpoint:]):
                writer.write(chunk)
                await writer.drain()
                await asyncio.sleep(0)
            return
        writer.write(data)
        await writer.drain()

    async def send_raw(self, writer: asyncio.StreamWriter, raw: bytes) -> None:
        writer.write(raw)
        await writer.drain()


@asynccontextmanager
async def connected_client(server: FakeQmpServer):
    """Start a server and return a ``QmpTransport`` connected to it."""
    listener = await asyncio.start_server(server.handle, "127.0.0.1", 0, limit=server.limit)
    port = listener.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port, limit=server.limit)
    transport = QmpTransport(reader, writer, name="test")
    try:
        yield transport
    finally:
        with contextlib.suppress(Exception):
            await transport.close()
        listener.close()
        with contextlib.suppress(Exception):
            await listener.wait_closed()
