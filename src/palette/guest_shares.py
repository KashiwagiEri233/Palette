"""WsgiDAV over spice-webdavd's private virtio multiplex channel (no host TCP port)."""

from __future__ import annotations

import asyncio
import contextlib
import io
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import h11
from wsgidav.wsgidav_app import WsgiDAVApp  # type: ignore[import-untyped]

from .config import SharedFolderConfig
from .errors import ErrorCode, PaletteError
from .guest_channel import GuestChannel

MAX_BODY = 16 * 1024 * 1024
MUX = struct.Struct("<QH")


@dataclass
class DavClient:
    connection: h11.Connection = field(
        default_factory=lambda: h11.Connection(h11.SERVER, max_incomplete_event_size=16384)
    )
    request: h11.Request | None = None
    body: bytearray = field(default_factory=bytearray)
    updated: float = field(default_factory=time.monotonic)


class GuestShares:
    def __init__(self, endpoint: str, folders: list[SharedFolderConfig]) -> None:
        self.channel = GuestChannel(endpoint)
        self.folders: list[SharedFolderConfig] = []
        self.app: Any = None
        self.clients: dict[int, DavClient] = {}
        self.active = False
        self.lock = asyncio.Lock()
        self.configure(folders)

    def configure(self, folders: list[SharedFolderConfig]) -> None:
        for folder in folders:
            if not Path(folder.path).is_dir():
                raise PaletteError(ErrorCode.PATH_DENIED, "共享目录不存在")
        self.app = WsgiDAVApp(
            {
                "provider_mapping": {
                    f"/{folder.tag}": {"root": folder.path, "readonly": folder.read_only}
                    for folder in folders
                },
                "simple_dc": {"user_mapping": {"*": True}},
                "fs_dav_provider": {"follow_symlinks": False},
                "dir_browser": {"enable": True},
                "verbose": 0,
                "logging": {"enable": False},
                "lock_storage": True,
                "property_manager": None,
            }
        )
        self.folders = list(folders)
        self.clients.clear()

    def status(self, guest_online: bool) -> dict[str, Any]:
        return {
            "available": self.channel.connected and guest_online,
            "url": "dav://127.0.0.1:9843/",
            "folders": [
                {"tag": folder.tag, "read_only": folder.read_only} for folder in self.folders
            ],
        }

    def _response(
        self, request: h11.Request, body: bytes
    ) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
        target = request.target.decode("ascii")
        url = urlsplit(target)
        if url.scheme or url.netloc:
            return 400, [], b"Invalid request target"
        headers = {
            key.decode("ascii").upper().replace("-", "_"): value.decode("latin1")
            for key, value in request.headers
        }
        environ: dict[str, Any] = {
            "REQUEST_METHOD": request.method.decode("ascii"),
            "SCRIPT_NAME": "",
            "PATH_INFO": unquote(url.path, encoding="latin1"),
            "QUERY_STRING": url.query,
            "SERVER_NAME": "localhost",
            "SERVER_PORT": "9843",
            "SERVER_PROTOCOL": "HTTP/1.1",
            "REMOTE_ADDR": "127.0.0.1",
            "wsgi.version": (1, 0),
            "wsgi.url_scheme": "http",
            "wsgi.input": io.BytesIO(body),
            "wsgi.errors": io.StringIO(),
            "wsgi.multithread": True,
            "wsgi.multiprocess": False,
            "wsgi.run_once": False,
            "CONTENT_LENGTH": str(len(body)),
            "CONTENT_TYPE": headers.get("CONTENT_TYPE", ""),
        }
        for key, value in headers.items():
            if key not in {"CONTENT_LENGTH", "CONTENT_TYPE"}:
                environ[f"HTTP_{key}"] = value
        response: list[Any] = []

        def start_response(
            status: str, values: list[tuple[str, str]], exc_info: Any = None
        ) -> None:
            response[:] = [
                int(status.split()[0]),
                [(key.encode("ascii"), value.encode("latin1")) for key, value in values],
            ]

        chunks = bytearray()
        iterable = self.app(environ, start_response)
        try:
            for chunk in iterable:
                if len(chunks) + len(chunk) > MAX_BODY:
                    return 413, [], b"Shared file exceeds 16 MiB limit"
                chunks.extend(chunk)
        finally:
            close = getattr(iterable, "close", None)
            if close is not None:
                close()
        status = int(response[0])
        if status >= 400:
            # Upstream error pages can contain filesystem details. The guest only
            # needs the HTTP status; never return diagnostic host paths.
            return status, [], b"Shared resource unavailable"
        return status, response[1], bytes(chunks)

    async def _send(self, client_id: int, data: bytes) -> None:
        for offset in range(0, len(data), 65535):
            block = data[offset : offset + 65535]
            await self.channel.write(MUX.pack(client_id, len(block)) + block)

    async def feed(self, client_id: int, data: bytes) -> None:
        async with self.lock:
            await self._feed(client_id, data)

    async def _feed(self, client_id: int, data: bytes) -> None:
        if not data:
            self.clients.pop(client_id, None)
            return
        now = time.monotonic()
        self.clients = {
            key: value for key, value in self.clients.items() if now - value.updated < 30
        }
        if client_id not in self.clients and len(self.clients) >= 4:
            await self.channel.write(MUX.pack(client_id, 0))
            return
        client = self.clients.setdefault(client_id, DavClient())
        client.updated = now
        try:
            client.connection.receive_data(data)
            while True:
                event = client.connection.next_event()
                if event is h11.NEED_DATA or event is h11.PAUSED:
                    return
                if isinstance(event, h11.Request):
                    client.request = event
                    if client.connection.they_are_waiting_for_100_continue:
                        await self._send(
                            client_id,
                            client.connection.send(
                                h11.InformationalResponse(status_code=100, headers=[])
                            )
                            or b"",
                        )
                elif isinstance(event, h11.Data):
                    client.body.extend(event.data)
                    if len(client.body) > MAX_BODY:
                        raise ValueError("request too large")
                elif isinstance(event, h11.EndOfMessage):
                    if client.request is None:
                        raise ValueError("missing request")
                    # Join the worker even on cancellation: an allowed write may already
                    # have started, and must finish before the share is reconfigured.
                    worker = asyncio.create_task(
                        asyncio.to_thread(self._response, client.request, bytes(client.body))
                    )
                    try:
                        status, headers, payload = await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        await worker
                        raise
                    content_length = (
                        next(
                            (value for key, value in headers if key.lower() == b"content-length"),
                            str(len(payload)).encode(),
                        )
                        if client.request.method == b"HEAD"
                        else str(len(payload)).encode()
                    )
                    headers = [
                        (key, value)
                        for key, value in headers
                        if key.lower()
                        not in {b"connection", b"transfer-encoding", b"content-length"}
                    ]
                    headers.append((b"Content-Length", content_length))
                    await self._send(
                        client_id,
                        client.connection.send(h11.Response(status_code=status, headers=headers))
                        or b"",
                    )
                    if client.request.method != b"HEAD":
                        await self._send(
                            client_id, client.connection.send(h11.Data(data=payload)) or b""
                        )
                    await self._send(client_id, client.connection.send(h11.EndOfMessage()) or b"")
                    if (
                        client.connection.our_state is h11.MUST_CLOSE
                        or client.connection.their_state is h11.MUST_CLOSE
                    ):
                        self.clients.pop(client_id, None)
                        await self.channel.write(MUX.pack(client_id, 0))
                        return
                    client.connection.start_next_cycle()
                    client.body.clear()
                    client.request = None
                elif isinstance(event, h11.ConnectionClosed):
                    self.clients.pop(client_id, None)
                    return
        except (ValueError, UnicodeError, h11.ProtocolError):
            self.clients.pop(client_id, None)
            await self.channel.write(MUX.pack(client_id, 0))

    async def run(self) -> None:
        while True:
            try:
                await self.channel.open()
                self.active = True
                while True:
                    client_id, size = MUX.unpack(await self.channel.readexactly(MUX.size))
                    await self.feed(client_id, await self.channel.readexactly(size))
            except TimeoutError:
                continue
            except (OSError, EOFError, ConnectionError):
                self.active = False
                self.clients.clear()
                await self.channel.close()
                await asyncio.sleep(1)

    async def close(self) -> None:
        self.active = False
        self.clients.clear()
        with contextlib.suppress(OSError):
            await self.channel.close()
