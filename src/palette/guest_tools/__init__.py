"""Guest tool bridge: protocol, disabled backend, mock backend and the transport seam."""

from __future__ import annotations

from .base import (
    DisabledGuestToolBackend,
    GuestCapabilities,
    GuestToolBackend,
    GuestToolOutcome,
    MockGuestToolBackend,
)
from .bridge import (
    BridgeGuestToolBackend,
    GuestToolTransport,
    MemoryGuestToolTransport,
    VirtioSerialGuestToolTransport,
)
from .protocol import PROTOCOL_VERSION, FrameDecoder, encode_frame, negotiate_version

__all__ = [
    "PROTOCOL_VERSION",
    "BridgeGuestToolBackend",
    "DisabledGuestToolBackend",
    "FrameDecoder",
    "GuestCapabilities",
    "GuestToolBackend",
    "GuestToolOutcome",
    "GuestToolTransport",
    "MemoryGuestToolTransport",
    "MockGuestToolBackend",
    "VirtioSerialGuestToolTransport",
    "encode_frame",
    "negotiate_version",
]
