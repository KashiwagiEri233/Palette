"""Framebuffer acquisition."""

from __future__ import annotations

from .base import CaptureBackend, CapturedImage
from .qmp import QmpCaptureBackend

__all__ = ["CaptureBackend", "CapturedImage", "QmpCaptureBackend"]
