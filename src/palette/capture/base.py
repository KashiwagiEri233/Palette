"""Capture backend contract.

An image is evidence, not proof. The backend reports exactly what it produced — native
framebuffer size, delivered image size, whether it had to shrink the picture — so that
the coordinate transform is always derived from the bytes the agent actually saw.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..models import Size


@dataclass(slots=True)
class CapturedImage:
    """One framebuffer snapshot, already encoded for delivery."""

    png_bytes: bytes
    native_size: Size
    image_size: Size
    format_used: str
    scaled: bool = False
    duration_ms: int = 0
    source_path: Path | None = None

    @property
    def byte_count(self) -> int:
        return len(self.png_bytes)


@runtime_checkable
class CaptureBackend(Protocol):
    """Swappable so that a VNC backend can replace QMP screendump without touching
    the input path or the tool contracts."""

    async def capture(self, *, display_id: str, head: int, sink: Path) -> CapturedImage: ...
