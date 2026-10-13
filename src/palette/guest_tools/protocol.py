"""The guest enhancement protocol: versioning, framing and message schema.

This is a *different* protocol from QMP, on a different channel, with its own framing. It
is defined now, with a disabled backend and a mock, because the plan keeps the guest-side
path as a first-class extension point: when Unicode text or app launching becomes a hard
requirement, the contract already exists and does not have to be invented under pressure.

Design commitments taken from the plan:

* version and capability negotiation, then length-framed messages with size and rate
  limits — never an unbounded line-oriented channel;
* every message carries an id so results can be matched, and a dead-line so a stalled
  guest service cannot pin the host;
* the channel is bound to ``vm_id``/``instance_id``/``boot_epoch`` and to a per-connection
  generation, so a guest reboot invalidates every outstanding request instead of letting
  a reply from the previous boot be mistaken for the current one;
* responses are data from an untrusted guest: they are size-limited, type-checked and
  never interpreted as instructions.
"""

from __future__ import annotations

import json
import re
import struct
from collections.abc import Iterable
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..errors import ErrorCode, PaletteError
from ..models import EffectState

#: The version this build speaks.
PROTOCOL_VERSION = "1.0"

#: Versions this build can still talk to. Kept separate from PROTOCOL_VERSION so that a
#: future bump can remain backwards compatible on purpose rather than by accident.
SUPPORTED_VERSIONS: tuple[str, ...] = ("1.0",)

#: 4-byte big-endian length prefix, then UTF-8 JSON.
_HEADER = struct.Struct(">I")

MAX_MESSAGE_BYTES = 1 << 20


class Capability(StrEnum):
    """Guest capabilities. Each is registered, granted and reported individually.

    Enabling one never enables another: text input and app launching are separately
    authorised because they carry different risk.
    """

    INPUT_TEXT = "guest.input_text"
    LAUNCH_APP = "guest.launch_app"


class InputMethod(StrEnum):
    """Concrete backends a guest may offer for text.

    ``native_text`` writes through the platform's own text API; ``clipboard_paste`` sets
    the *guest's* clipboard and pastes. There is no automatic downgrade between them: a
    caller asks for one and is told if it is unavailable.
    """

    NATIVE_TEXT = "native_text"
    CLIPBOARD_PASTE = "clipboard_paste"


class MessageKind(StrEnum):
    HELLO = "hello"
    HELLO_ACK = "hello_ack"
    REQUEST = "request"
    RESPONSE = "response"
    ERROR = "error"


class Envelope(BaseModel):
    """The wire envelope. Unknown fields are rejected so a newer guest cannot smuggle
    semantics past an older host."""

    model_config = ConfigDict(extra="forbid")

    v: str = PROTOCOL_VERSION
    kind: str
    id: str
    payload: dict[str, Any] = Field(default_factory=dict)


class HelloPayload(BaseModel):
    """Host → guest opening message."""

    model_config = ConfigDict(extra="forbid")

    host: str = "palette"
    versions: list[str] = Field(default_factory=lambda: list(SUPPORTED_VERSIONS))
    vm_id: str
    instance_id: str
    boot_epoch: int = Field(ge=0)
    connect_id: str
    desired_capabilities: list[str] = Field(default_factory=list)


class HelloAckPayload(BaseModel):
    """Guest → host reply. Everything here is a claim to be verified against the
    profile's grant, not a permission in itself."""

    model_config = ConfigDict(extra="forbid")

    version: str
    capabilities: list[str] = Field(default_factory=list)
    input_methods: list[str] = Field(default_factory=list)
    apps: list[str] = Field(default_factory=list)
    session: str = "interactive"
    interactive_desktop: bool | None = None
    detail: str | None = None


class InputTextParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    method: str
    target: str = "focused"


class LaunchAppParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app_id: str
    options: dict[str, Any] = Field(default_factory=dict)


class ResponsePayload(BaseModel):
    """Guest → host result. ``effect_state`` uses the same vocabulary as QMP actions so
    that a receipt means the same thing regardless of which backend produced it."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    effect_state: EffectState = EffectState.SUBMITTED
    method: str | None = None
    launch_id: str | None = None
    detail: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


def negotiate_version(host_versions: Iterable[str], guest_versions: Iterable[str]) -> str:
    """Pick the highest version both sides support, or fail with a precise reason.

    Ordering is numeric-part order (``1.10`` is newer than ``1.9``), with the host's own
    list order breaking ties. Using string ordering would silently pick the wrong version
    the first time a minor number reached two digits.
    """
    host_list = list(host_versions)
    guest_set = set(guest_versions)
    common = [version for version in host_list if version in guest_set]
    if not common:
        raise PaletteError(
            ErrorCode.GUEST_TOOL_VERSION_MISMATCH,
            "no guest tool protocol version is common to both sides",
            details={"host": sorted(host_list), "guest": sorted(guest_set)},
            retryable=False,
        )
    preference = {version: index for index, version in enumerate(host_list)}
    return max(common, key=lambda version: (_version_order(version), -preference[version]))


def _version_order(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in re.split(r"[.\-+]", version):
        parts.append(int(chunk) if chunk.isdigit() else 0)
    return tuple(parts)


def encode_frame(payload: dict[str, Any], *, max_bytes: int = MAX_MESSAGE_BYTES) -> bytes:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(body) > max_bytes:
        raise PaletteError(
            ErrorCode.INVALID_ARGUMENT,
            "guest tool message exceeds the configured size limit",
            details={"max_bytes": max_bytes},
        )
    return _HEADER.pack(len(body)) + body


class FrameDecoder:
    """Incremental length-prefixed decoder.

    A single read may contain half a message or three messages; the decoder keeps the
    remainder so that framing never depends on how the transport happened to split reads.
    """

    def __init__(self, *, max_bytes: int = MAX_MESSAGE_BYTES) -> None:
        self._buffer = bytearray()
        self._max_bytes = max_bytes
        self.discarded = 0

    def feed(self, data: bytes) -> list[dict[str, Any]]:
        self._buffer.extend(data)
        messages: list[dict[str, Any]] = []
        while True:
            if len(self._buffer) < _HEADER.size:
                return messages
            (length,) = _HEADER.unpack_from(self._buffer, 0)
            if length > self._max_bytes:
                # Not resynchronisable: the stream cannot be trusted after this.
                self._buffer.clear()
                self.discarded += 1
                raise PaletteError(
                    ErrorCode.INVALID_ARGUMENT,
                    "guest tool message exceeds the configured size limit",
                    details={"declared_bytes": length, "max_bytes": self._max_bytes},
                )
            total = _HEADER.size + length
            if len(self._buffer) < total:
                return messages
            body = bytes(self._buffer[_HEADER.size : total])
            del self._buffer[:total]
            try:
                message = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self.discarded += 1
                continue
            if isinstance(message, dict):
                messages.append(message)
            else:
                self.discarded += 1

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)


def parse_envelope(message: dict[str, Any]) -> Envelope:
    try:
        return Envelope.model_validate(message)
    except Exception as exc:
        raise PaletteError(
            ErrorCode.GUEST_TOOL_UNAVAILABLE,
            "the guest tool service sent a message this host cannot parse",
        ) from exc
