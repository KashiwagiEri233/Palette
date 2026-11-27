"""Experimental, host-owned framebuffer. Never supplies authoritative action frames.

Only complete immutable RGBA images cross the subscriber boundary. A slow viewer may
skip complete images, but never an update needed to reconstruct a later image.
"""

from __future__ import annotations

import asyncio
import io
import json
import struct
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from PIL import Image

from .capture.base import CapturedImage
from .capture.work import image_work
from .config import CaptureConfig
from .errors import ErrorCode, PaletteError
from .models import Size

MAX_PIXELS = 8192 * 4320
MAX_PACKET = MAX_PIXELS * 4 + 131072


@dataclass(frozen=True, slots=True)
class SourceFrame:
    instance_id: str
    generation: str
    seq: int
    width: int
    height: int
    rgba: bytes
    received_at: float
    cursor: Mapping[str, Any] | None = None
    update_rect: tuple[int, int, int, int] | None = None
    update_rgba: bytes | None = None
    cursor_only: bool = False

    def _encode_wire(self, payload: bytes, **extra: Any) -> bytes:
        header = json.dumps(
            {
                "version": 1,
                "instance_id": self.instance_id,
                "generation": self.generation,
                "seq": self.seq,
                "width": self.width,
                "height": self.height,
                "format": "rgba",
                "cursor": dict(self.cursor) if self.cursor else None,
                **extra,
            },
            separators=(",", ":"),
        ).encode()
        return struct.pack(">I", len(header)) + header + payload

    def wire(self) -> bytes:
        return self._encode_wire(self.rgba)

    def wire_for(self, last_sent_seq: int | None = None) -> bytes:
        if last_sent_seq is None or self.seq != last_sent_seq + 1:
            return self.wire()
        if self.cursor_only:
            return self._encode_wire(b"", kind="cursor")
        if self.update_rect is not None and self.update_rgba is not None:
            return self._encode_wire(self.update_rgba, kind="rect", rect=list(self.update_rect))
        return self.wire()


class FrameSource:
    def __init__(self, instance_id: str) -> None:
        self.instance_id = instance_id
        self.generation = uuid.uuid4().hex
        self.frame: SourceFrame | None = None
        self.closed = False
        self.invalidated = asyncio.Event()
        self._seq = 0
        self._cursor: dict[str, Any] | None = None
        self._subscribers: set[asyncio.Queue[SourceFrame | None]] = set()

    def subscribe(self) -> asyncio.Queue[SourceFrame | None]:
        if len(self._subscribers) >= 8:
            raise PaletteError(ErrorCode.BUSY, "too many raw display consumers")
        queue: asyncio.Queue[SourceFrame | None] = asyncio.Queue(maxsize=1)
        self._subscribers.add(queue)
        if self.closed:
            queue.put_nowait(None)
        elif self.frame is not None:
            queue.put_nowait(self.frame)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[SourceFrame | None]) -> None:
        self._subscribers.discard(queue)

    async def wait_frame(self, timeout: float = 1) -> SourceFrame | None:
        queue = self.subscribe()
        try:
            async with asyncio.timeout(timeout):
                while not self.closed:
                    frame = await queue.get()
                    if frame is not None:
                        return frame
        except TimeoutError:
            pass
        finally:
            self.unsubscribe(queue)
        return None

    def _publish(self, frame: SourceFrame | None) -> None:
        for queue in self._subscribers:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(frame)

    def apply(self, header: dict[str, Any], data: bytes) -> None:
        if self.closed:
            raise ValueError("frame source is closed")
        kind = header.get("kind")
        previous = self.frame
        update_rect: tuple[int, int, int, int] | None = None
        update_rgba: bytes | None = None
        cursor_only = False
        if kind == "disable":
            self.frame = None
            self.invalidated.set()
            self._publish(None)
            return
        if kind == "cursor":
            if data or len(json.dumps(header)) > 131072:
                raise ValueError("invalid cursor")
            self._cursor = {**(self._cursor or {}), **header}
            if previous is None:
                return
            width, height, rgba = previous.width, previous.height, previous.rgba
            cursor_only = True
        else:
            raw_width, raw_height = header.get("width"), header.get("height")
            if (
                type(raw_width) is not int
                or type(raw_height) is not int
                or not 0 < raw_width <= 8192
                or not 0 < raw_height <= 4320
                or raw_width * raw_height > MAX_PIXELS
                or len(data) != raw_width * raw_height * 4
            ):
                raise ValueError("invalid raw framebuffer dimensions or length")
            width, height = int(raw_width), int(raw_height)
            if kind == "scanout":
                rgba = bytes(data)
                self.invalidated.clear()
            elif kind == "update":
                x, y = header.get("x"), header.get("y")
                if (
                    previous is None
                    or type(x) is not int
                    or type(y) is not int
                    or x < 0
                    or y < 0
                    or x + width > previous.width
                    or y + height > previous.height
                ):
                    raise ValueError(
                        f"raw update {(x, y, width, height)} is outside scanout "
                        f"{(previous.width, previous.height) if previous else None}"
                    )
                if width == previous.width and height == previous.height:
                    rgba = bytes(data)
                else:
                    buffer = bytearray(previous.rgba)
                    row_bytes = width * 4
                    buf_view = memoryview(buffer)
                    src_view = memoryview(data)
                    if width == previous.width:
                        start = y * previous.width * 4
                        buf_view[start : start + height * row_bytes] = src_view
                    else:
                        for row in range(height):
                            offset = ((y + row) * previous.width + x) * 4
                            buf_view[offset : offset + row_bytes] = src_view[
                                row * row_bytes : (row + 1) * row_bytes
                            ]
                    rgba = bytes(buffer)
                    update_rect = (x, y, width, height)
                    update_rgba = bytes(data)
                width, height = previous.width, previous.height
            else:
                raise ValueError("unknown raw display message")
        self._seq += 1
        self.frame = SourceFrame(
            self.instance_id,
            self.generation,
            self._seq,
            width,
            height,
            rgba,
            time.monotonic(),
            MappingProxyType(dict(self._cursor)) if self._cursor else None,
            update_rect=update_rect,
            update_rgba=update_rgba,
            cursor_only=cursor_only,
        )
        self._publish(self.frame)

    async def shadow_capture(self, config: CaptureConfig) -> CapturedImage:
        """Copy a specific source version; it is not registered in FrameRegistry."""
        frame = self.frame
        if self.closed or frame is None:
            raise PaletteError(ErrorCode.CAPTURE_FAILED, "experimental display has no image")
        started = time.perf_counter()

        def encode() -> CapturedImage:
            image = Image.frombytes("RGBA", (frame.width, frame.height), frame.rgba).convert("RGB")
            image.thumbnail((config.max_long_edge, config.max_long_edge), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            image.save(output, format="PNG", compress_level=1)
            if output.tell() > config.max_bytes:
                output = io.BytesIO()
                image.save(output, format="PNG", optimize=True)
            if output.tell() > config.max_bytes:
                raise PaletteError(ErrorCode.CAPTURE_TOO_LARGE, "shadow PNG exceeds capture limit")
            return CapturedImage(
                png_bytes=output.getvalue(),
                native_size=Size(width=frame.width, height=frame.height),
                image_size=Size(width=image.width, height=image.height),
                format_used="dbus-shadow",
                scaled=image.size != (frame.width, frame.height),
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

        return await image_work(encode)

    def close(self) -> None:
        self.closed = True
        self.invalidated.set()
        self.frame = None
        self._publish(None)


async def read_bridge_packet(reader: asyncio.StreamReader) -> tuple[dict[str, Any], bytes]:
    size = struct.unpack(">I", await reader.readexactly(4))[0]
    if not 4 <= size <= MAX_PACKET:
        raise ValueError("invalid bridge packet size")
    header_size = struct.unpack(">I", await reader.readexactly(4))[0]
    if not 0 < header_size <= min(131072, size - 4):
        raise ValueError("invalid bridge header size")
    header = json.loads(await reader.readexactly(header_size))
    if not isinstance(header, dict):
        raise ValueError("invalid bridge header")
    return header, await reader.readexactly(size - 4 - header_size)
