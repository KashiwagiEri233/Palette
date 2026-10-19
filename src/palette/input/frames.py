"""Bounded frame authority, resizing and visual change detection."""

from __future__ import annotations

import io
from collections import OrderedDict

from PIL import Image

from ..capture.base import CapturedImage
from ..errors import ErrorCode, PaletteError
from ..models import Frame, Size

_THUMBNAIL_SIZE = (64, 64)


class FrameRegistry:
    """A small, bounded store of frames the agent may still act on.

    Retired identifiers are remembered briefly and with a reason. That distinction
    matters to the agent: "the display changed while you were looking at that frame" is a
    different situation, with a different recovery, from "that identifier was never
    issued". Both are refusals, but only one of them means "observe again and retry".
    """

    def __init__(self, *, max_entries: int = 16, max_retired: int = 32) -> None:
        self._frames: OrderedDict[str, Frame] = OrderedDict()
        #: One small grayscale thumbnail per frame. Frames themselves are never retained,
        #: but "has the screen changed since the picture you saw?" needs *something* to
        #: compare against, and a 64x64 thumbnail is a few kilobytes rather than a
        #: megabyte of someone's desktop.
        self._thumbnails: OrderedDict[str, bytes] = OrderedDict()
        self._retired: OrderedDict[str, str] = OrderedDict()
        self._max_entries = max_entries
        self._max_retired = max_retired

    def add(self, frame: Frame, thumbnail: bytes | None = None) -> Frame:
        self._frames[frame.frame_id] = frame
        if thumbnail is not None:
            self._thumbnails[frame.frame_id] = thumbnail
        while len(self._frames) > self._max_entries:
            evicted, _ = self._frames.popitem(last=False)
            self._thumbnails.pop(evicted, None)
        return frame

    def thumbnail(self, frame_id: str) -> bytes | None:
        return self._thumbnails.get(frame_id)

    def get(self, frame_id: str) -> Frame:
        frame = self._frames.get(frame_id)
        if frame is not None:
            return frame
        reason = self._retired.get(frame_id)
        if reason is not None:
            raise PaletteError(
                ErrorCode.STALE_FRAME,
                f"this frame was retired because {reason}; observe again",
                details={"frame_id": frame_id, "reason": reason},
            )
        raise PaletteError(
            ErrorCode.FRAME_NOT_FOUND,
            "that frame is not known to this instance; observe again before acting",
            details={"frame_id": frame_id},
        )

    def latest(self) -> Frame | None:
        if not self._frames:
            return None
        return list(self._frames.values())[-1]

    def clear(self, *, reason: str = "the display changed") -> None:
        for frame_id in self._frames:
            self._retired[frame_id] = reason
        self._frames.clear()
        self._thumbnails.clear()
        while len(self._retired) > self._max_retired:
            self._retired.popitem(last=False)


def rescale(captured: CapturedImage, max_long_edge: int, max_bytes: int) -> CapturedImage:
    """Apply a caller's image-size preference without hiding the transform change."""
    with Image.open(io.BytesIO(captured.png_bytes)) as image:
        longest = max(image.width, image.height)
        if longest <= max_long_edge:
            return captured
        ratio = max_long_edge / longest
        width = max(2, int(image.width * ratio))
        height = max(2, int(image.height * ratio))
        resized = image.convert("RGB").resize((width, height), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        resized.save(buffer, format="PNG", optimize=True)
    payload = buffer.getvalue()
    if len(payload) > max_bytes:
        raise PaletteError(
            ErrorCode.CAPTURE_TOO_LARGE,
            "the resized image still exceeds the configured byte limit",
        )
    return CapturedImage(
        png_bytes=payload,
        native_size=captured.native_size,
        image_size=Size(width=width, height=height),
        format_used=captured.format_used,
        scaled=True,
        duration_ms=captured.duration_ms,
        source_path=captured.source_path,
    )


def image_thumbnail(png_bytes: bytes, *, size: tuple[int, int] = _THUMBNAIL_SIZE) -> bytes:
    """A small grayscale reduction of a frame, used only for change detection."""
    try:
        with Image.open(io.BytesIO(png_bytes)) as image:
            return image.convert("L").resize(size).tobytes()
    except Exception:
        return b""


def thumbnail_difference(left: bytes, right: bytes) -> float:
    """Mean absolute difference in 0..1 between two thumbnails."""
    if not left or not right or len(left) != len(right):
        return 1.0
    total = sum(abs(a - b) for a, b in zip(left, right, strict=True))
    return total / (255 * len(left))


def image_difference_ratio(
    left: bytes, right: bytes, *, size: tuple[int, int] = _THUMBNAIL_SIZE
) -> float:
    """Mean absolute pixel difference in 0..1 between two PNGs.

    Downscaled to a thumbnail first so that the heuristic is cheap and insensitive to
    single-pixel noise. It is a heuristic: the plan is explicit that pixel stability is
    not proof that an application is idle.
    """
    try:
        with Image.open(io.BytesIO(left)) as a, Image.open(io.BytesIO(right)) as b:
            if a.size != b.size:
                return 1.0
            left_thumb = a.convert("L").resize(size)
            right_thumb = b.convert("L").resize(size)
            left_bytes = left_thumb.tobytes()
            right_bytes = right_thumb.tobytes()
    except Exception:
        return 1.0
    return thumbnail_difference(left_bytes, right_bytes)
