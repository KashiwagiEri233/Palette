"""Bounded JSON messages for the local operator channel; never pickle or raw RPC."""

from __future__ import annotations

import asyncio
import json
import struct
from typing import Annotated, Any, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..errors import ErrorCode, PaletteError
from ..models import ErrorPayload

MAX_FRAME_BYTES = 256 * 1024
CONNECT_TIMEOUT = 2.0
FRAME_TIMEOUT = 5.0
OPERATION_TIMEOUT = 180.0
MAX_CLIENTS = 8
_HEADER = struct.Struct(">I")


class Channel(Protocol):
    async def readexactly(self, size: int) -> bytes: ...
    async def write(self, data: bytes) -> None: ...
    async def close(self) -> None: ...


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Start(StrictModel):
    method: Literal["start"]
    vm_id: str = Field(min_length=1, max_length=128)
    profile_id: str | None = None
    image: str | None = None
    image_format: Literal["qcow2", "raw"] | None = None
    iso: list[str] | None = None
    extra_disks: list[str] | None = None
    qemu_args: list[str] | None = None


class Stop(StrictModel):
    method: Literal["stop"]
    vm_id: str = Field(min_length=1, max_length=128)
    force: bool = False


class Reset(StrictModel):
    method: Literal["reset"]
    vm_id: str = Field(min_length=1, max_length=128)
    profile_id: str | None = None


class Status(StrictModel):
    method: Literal["status"]
    vm_id: str = Field(min_length=1, max_length=128)


class EmergencyStop(StrictModel):
    method: Literal["emergency_stop"]
    vm_id: str | None = Field(default=None, max_length=128)


class TakeControl(StrictModel):
    method: Literal["take_control"]
    vm_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=4096)


class DesktopCommand(StrictModel):
    method: Literal[
        "hello",
        "profiles",
        "image.info",
        "doctor",
        "vms.list",
        "vms.create",
        "vms.update",
        "vms.settings.get",
        "vms.disk.resize",
        "hardware.capabilities",
        "vms.start",
        "vms.stop",
        "vms.restart",
        "vms.reset",
        "vms.delete",
        "display.ticket",
        "display.sync",
        "control.acquire",
        "control.input",
        "control.heartbeat",
        "control.release",
        "daemon.stop",
        "runtime.stop",
    ]
    vm_id: str | None = Field(default=None, max_length=128)
    payload: dict[str, Any] = Field(default_factory=dict)


Command = Annotated[
    Start | Stop | Reset | Status | EmergencyStop | TakeControl | DesktopCommand,
    Field(discriminator="method"),
]


class Request(StrictModel):
    version: Literal[1] = 1
    generation: str = Field(pattern=r"^[a-f0-9]{32}$")
    token: str = Field(pattern=r"^[a-f0-9]{64}$")
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    command: Command


class Response(StrictModel):
    version: Literal[1] = 1
    generation: str
    request_id: str
    ok: bool
    result: dict[str, Any] | None = None
    error: ErrorPayload | None = None

    @model_validator(mode="after")
    def _payload_matches_status(self) -> Self:
        if self.ok != (self.result is not None) or self.ok == (self.error is not None):
            raise ValueError("operator response must have exactly its success or error payload")
        return self


class Endpoint(StrictModel):
    version: Literal[1] = 1
    transport: Literal["unix", "pipe"]
    address: str = Field(min_length=1, max_length=512)
    generation: str = Field(pattern=r"^[a-f0-9]{32}$")
    token: str = Field(pattern=r"^[a-f0-9]{64}$")
    operation_timeout_seconds: float = Field(default=OPERATION_TIMEOUT, gt=0)


def decode_json(data: bytes) -> dict[str, Any]:
    def reject_constant(value: str) -> Any:
        raise ValueError(f"non-finite JSON number: {value}")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = value
        return result

    try:
        result = json.loads(data, parse_constant=reject_constant, object_pairs_hook=unique_object)
    except RecursionError as exc:
        raise ValueError("operator JSON exceeds the nesting limit") from exc
    if not isinstance(result, dict):
        raise ValueError("operator messages must be JSON objects")
    return result


async def receive_frame(channel: Channel, *, timeout: float = FRAME_TIMEOUT) -> bytes:
    # One deadline covers header and body, including a peer trickling partial bytes.
    async with asyncio.timeout(timeout):
        (size,) = _HEADER.unpack(await channel.readexactly(_HEADER.size))
        if not 0 < size <= MAX_FRAME_BYTES:
            raise PaletteError(ErrorCode.INVALID_ARGUMENT, "invalid operator message length")
        return await channel.readexactly(size)


async def send_frame(channel: Channel, payload: BaseModel) -> None:
    body = json.dumps(
        payload.model_dump(mode="json"), allow_nan=False, separators=(",", ":")
    ).encode("utf-8")
    if not 0 < len(body) <= MAX_FRAME_BYTES:
        raise PaletteError(ErrorCode.INVALID_ARGUMENT, "operator message exceeds its size limit")
    async with asyncio.timeout(FRAME_TIMEOUT):
        await channel.write(_HEADER.pack(len(body)) + body)
