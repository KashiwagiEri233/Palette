"""Backend-only RFB extensions for Unicode clipboard and virtio-gpu UI size hints.

The renderer display gateway stays read-only. This authenticated private client sends
only bounded clipboard/resize messages after runtime ownership checks.
"""

from __future__ import annotations

import asyncio
import contextlib
import struct
import zlib
from typing import Any

from .display import VERSION, vnc_response
from .errors import ErrorCode, PaletteError

TEXT = 1
CAPS = 1 << 24
REQUEST = 1 << 25
NOTIFY = 1 << 27
PROVIDE = 1 << 28


def clipboard_packet(flags: int, data: bytes = b"") -> bytes:
    payload = struct.pack(">I", flags) + data
    return struct.pack(">B3xi", 6, -len(payload)) + payload


def decode_clipboard(data: bytes, limit: int) -> str:
    decoder = zlib.decompressobj()
    decoded = decoder.decompress(data, limit + 6)
    if len(decoded) > limit + 5 or not decoder.eof or decoder.unused_data or len(decoded) < 4:
        raise ValueError("clipboard exceeds its limit or has invalid compression")
    (size,) = struct.unpack_from(">I", decoded)
    if size != len(decoded) - 4 or size > limit + 1:
        raise ValueError("clipboard text size is invalid")
    text = decoded[4:]
    if text.endswith(b"\0"):
        text = text[:-1]
    return text.decode("utf-8").replace("\r\n", "\n")


class RfbGuestDesktop:
    def __init__(
        self, address: tuple[str, int], password: str, *, max_clipboard_bytes: int = 48 * 1024
    ) -> None:
        self.address = address
        self.password = password
        self.limit = max_clipboard_bytes
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.task: asyncio.Task[None] | None = None
        self.ready = asyncio.Event()
        self.clipboard: str | None = None
        self.clipboard_reply: asyncio.Future[str] | None = None
        self.resize_reply: asyncio.Future[int] | None = None
        self.resize_target: tuple[int, int] | None = None
        self.lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self.writer is not None and self.task is not None and not self.task.done()

    async def connect(self) -> None:
        await self.close()
        try:
            async with asyncio.timeout(3):
                self.reader, self.writer = await asyncio.open_connection(*self.address)
                if await self.reader.readexactly(12) != VERSION:
                    raise ValueError("RFB 3.8 required")
                self.writer.write(VERSION)
                count = (await self.reader.readexactly(1))[0]
                if not 0 < count <= 32 or 2 not in await self.reader.readexactly(count):
                    raise ValueError("private VNC authentication required")
                self.writer.write(b"\x02")
                self.writer.write(vnc_response(self.password, await self.reader.readexactly(16)))
                await self.writer.drain()
                if await self.reader.readexactly(4) != bytes(4):
                    raise ValueError("VNC authentication failed")
                self.writer.write(b"\x01")
                await self.writer.drain()
                header = await self.reader.readexactly(24)
                length = struct.unpack_from(">I", header, 20)[0]
                if length > 4096:
                    raise ValueError("VNC name too long")
                await self.reader.readexactly(length)
                self.writer.write(struct.pack(">BBHiii", 2, 0, 3, 0, -309, -308))
                await self.writer.drain()
                self.task = asyncio.create_task(self._receive(), name="guest-rfb")
                await self.ready.wait()
        except BaseException:
            await self.close()
            raise

    async def _write(self, data: bytes) -> None:
        if not self.connected or self.writer is None:
            raise PaletteError(ErrorCode.GUEST_TOOL_UNAVAILABLE, "桌面增强通道尚未连接")
        self.writer.write(data)
        await self.writer.drain()

    async def clipboard_set(self, text: str) -> dict[str, Any]:
        try:
            data = text.replace("\r\n", "\n").encode("utf-8")
        except UnicodeError as exc:
            raise PaletteError(ErrorCode.INVALID_ARGUMENT, "文本编码无效") from exc
        if len(data) > self.limit or b"\0" in data:
            raise PaletteError(ErrorCode.INVALID_ARGUMENT, "剪贴板文本过长或包含空字符")
        async with self.lock:
            payload = data + b"\0"
            await self._write(clipboard_packet(NOTIFY | TEXT))
            await self._write(
                clipboard_packet(
                    PROVIDE | TEXT, zlib.compress(struct.pack(">I", len(payload)) + payload)
                )
            )
            self.clipboard = text
            return {"submitted": True}

    async def clipboard_get(self) -> dict[str, Any]:
        async with self.lock:
            if self.clipboard is not None:
                return {"text": self.clipboard}
            future = asyncio.get_running_loop().create_future()
            self.clipboard_reply = future
            try:
                await self._write(clipboard_packet(REQUEST | TEXT))
                async with asyncio.timeout(3):
                    return {"text": await future}
            except TimeoutError as exc:
                raise PaletteError(
                    ErrorCode.GUEST_TOOL_UNAVAILABLE, "尚未收到客户机剪贴板, 请在客户机中重新复制"
                ) from exc
            finally:
                self.clipboard_reply = None

    async def resize(self, width: int, height: int) -> dict[str, Any]:
        if (
            type(width) is not int
            or type(height) is not int
            or not (320 <= width <= 8192 and 200 <= height <= 8192)
            or width * height > 16_777_216
        ):
            raise PaletteError(ErrorCode.INVALID_ARGUMENT, "分辨率超出允许范围")
        async with self.lock:
            future = asyncio.get_running_loop().create_future()
            self.resize_reply = future
            self.resize_target = (width, height)
            try:
                await self._write(
                    struct.pack(
                        ">BBHHBBIHHHHI", 251, 0, width, height, 1, 0, 0, 0, 0, width, height, 0
                    )
                )
                async with asyncio.timeout(3):
                    status = await future
                if status not in {0, 4}:
                    raise PaletteError(
                        ErrorCode.UNSUPPORTED_CAPABILITY, "客户机显卡拒绝了分辨率请求"
                    )
                return {
                    "submitted": True,
                    "width": width,
                    "height": height,
                    "confirmed": status == 0,
                }
            except TimeoutError as exc:
                raise PaletteError(
                    ErrorCode.ACTION_OUTCOME_UNKNOWN, "分辨率请求结果未知, 请检查实际画面"
                ) from exc
            finally:
                self.resize_reply = None
                self.resize_target = None

    async def _receive(self) -> None:
        assert self.reader is not None  # noqa: S101
        try:
            while True:
                kind = (await self.reader.readexactly(1))[0]
                if kind == 3:
                    header = await self.reader.readexactly(7)
                    length = struct.unpack_from(">i", header, 3)[0]
                    if abs(length) > self.limit + 1024 or length > 0:
                        raise ValueError("unsupported clipboard format")
                    payload = await self.reader.readexactly(-length)
                    if len(payload) < 4:
                        raise ValueError("invalid clipboard packet")
                    flags = struct.unpack_from(">I", payload)[0]
                    if flags & CAPS:
                        await self._write(
                            clipboard_packet(
                                CAPS | REQUEST | NOTIFY | PROVIDE | TEXT,
                                struct.pack(">I", self.limit),
                            )
                        )
                        self.ready.set()
                    elif flags & NOTIFY:
                        self.clipboard = None if flags & TEXT else ""
                    elif flags & PROVIDE and flags & TEXT:
                        self.clipboard = decode_clipboard(payload[4:], self.limit)
                        if self.clipboard_reply is not None and not self.clipboard_reply.done():
                            self.clipboard_reply.set_result(self.clipboard)
                elif kind == 0:
                    _, count = struct.unpack(">BH", await self.reader.readexactly(3))
                    if count > 64:
                        raise ValueError("too many rectangles")
                    for _index in range(count):
                        _x, status, width, height, encoding = struct.unpack(
                            ">HHHHi", await self.reader.readexactly(12)
                        )
                        if encoding == -308:
                            screens = (await self.reader.readexactly(4))[0]
                            if screens > 16:
                                raise ValueError("too many screens")
                            await self.reader.readexactly(screens * 16)
                            if (
                                self.resize_reply is not None
                                and not self.resize_reply.done()
                                and (status != 0 or self.resize_target == (width, height))
                            ):
                                self.resize_reply.set_result(status)
                        elif encoding == 0 and width * height <= 16_777_216:
                            remaining = width * height * 4
                            while remaining:
                                chunk = min(65536, remaining)
                                await self.reader.readexactly(chunk)
                                remaining -= chunk
                        else:
                            raise ValueError("unexpected private RFB encoding")
                elif kind != 2:
                    raise ValueError("unexpected private RFB message")
        except (OSError, EOFError, ValueError, zlib.error, UnicodeError, PaletteError):
            self.ready.clear()
            self.clipboard = None
            for future in (self.clipboard_reply, self.resize_reply):
                if future is not None and not future.done():
                    future.set_exception(
                        PaletteError(ErrorCode.ACTION_OUTCOME_UNKNOWN, "桌面增强通道已断开")
                    )

    async def close(self) -> None:
        task, self.task = self.task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        writer, self.writer = self.writer, None
        if writer is not None:
            writer.close()
            with contextlib.suppress(OSError, TimeoutError):
                async with asyncio.timeout(1):
                    await writer.wait_closed()
        self.reader = None
        self.ready.clear()
        self.clipboard = None
