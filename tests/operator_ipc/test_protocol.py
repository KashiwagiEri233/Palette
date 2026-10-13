"""Wire validation and whole-frame deadlines for the operator channel."""

from __future__ import annotations

import asyncio
import struct

import pytest
from pydantic import ValidationError

from palette.errors import PaletteError
from palette.operator.protocol import MAX_FRAME_BYTES, Request, receive_frame


class ReaderChannel:
    def __init__(self) -> None:
        self.reader = asyncio.StreamReader()
        self.read_sizes: list[int] = []

    async def readexactly(self, size: int) -> bytes:
        self.read_sizes.append(size)
        return await self.reader.readexactly(size)

    async def write(self, data: bytes) -> None:
        self.reader.feed_data(data)

    async def close(self) -> None:
        self.reader.feed_eof()


def request(**command):
    return {"generation": "a" * 32, "token": "b" * 64, "request_id": "c" * 32, "command": command}


@pytest.mark.parametrize(
    "command",
    [
        {"method": "execute", "command": "quit"},
        {"method": "stop", "vm_id": "desktop", "actor": "operator"},
        {"method": "stop", "vm_id": "desktop", "force": "false"},
    ],
)
def test_only_explicit_typed_commands_are_accepted(command) -> None:
    with pytest.raises(ValidationError):
        Request.model_validate(request(**command))


async def test_declared_length_is_checked_before_reading_the_body() -> None:
    channel = ReaderChannel()
    await channel.write(struct.pack(">I", MAX_FRAME_BYTES + 1))
    with pytest.raises(PaletteError):
        await receive_frame(channel)
    assert channel.read_sizes == [4]


async def test_partial_frames_have_one_deadline() -> None:
    channel = ReaderChannel()
    await channel.write(struct.pack(">I", 100) + b"{")
    with pytest.raises(TimeoutError):
        await receive_frame(channel, timeout=0.02)


async def test_fragmented_and_concatenated_frames_keep_their_boundaries() -> None:
    channel = ReaderChannel()
    first = asyncio.create_task(receive_frame(channel))
    await channel.write(b"\0\0")
    await asyncio.sleep(0)
    await channel.write(b"\0\x02{}\0\0\0\x02[]")
    assert await first == b"{}"
    assert await receive_frame(channel) == b"[]"
