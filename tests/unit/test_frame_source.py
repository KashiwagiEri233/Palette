"""Complete-frame publication, bounded consumers and shadow evidence separation."""

import asyncio
import io
import json
import struct

import pytest
from PIL import Image

from palette.config import CaptureConfig, HostConfig
from palette.frame_source import FrameSource, read_bridge_packet


def scanout(source: FrameSource, width: int = 3, height: int = 2, value: int = 0) -> None:
    source.apply(
        {"kind": "scanout", "width": width, "height": height}, bytes([value]) * width * height * 4
    )


async def test_delta_is_composed_before_slow_subscribers_drop_frames() -> None:
    source = FrameSource("inst_example")
    queue = source.subscribe()
    scanout(source)
    before = source.frame
    source.apply({"kind": "update", "x": 1, "y": 0, "width": 1, "height": 2}, b"\xff" * 8)
    source.apply({"kind": "update", "x": 2, "y": 1, "width": 1, "height": 1}, b"\xaa" * 4)
    assert queue.qsize() == 1
    latest = await queue.get()
    assert before is not None and latest is not None
    assert before.rgba == b"\0" * 24
    assert latest.rgba == b"\0" * 4 + b"\xff" * 4 + b"\0" * 8 + b"\xff" * 4 + b"\xaa" * 4
    assert latest.seq == 3
    source.unsubscribe(queue)
    source.close()


@pytest.mark.parametrize(
    "header,data",
    [
        ({"kind": "scanout", "width": 9000, "height": 1}, b""),
        ({"kind": "scanout", "width": True, "height": 1}, b"\0" * 4),
        ({"kind": "scanout", "width": 3, "height": 2}, b"\0" * 8),
        ({"kind": "update", "x": 2, "y": 1, "width": 2, "height": 1}, b"\0" * 8),
        ({"kind": "update", "x": -1, "y": 0, "width": 1, "height": 1}, b"\0" * 4),
    ],
)
def test_bad_pixels_do_not_replace_the_last_complete_frame(header, data) -> None:
    source = FrameSource("inst_example")
    scanout(source)
    previous = source.frame
    with pytest.raises(ValueError):
        source.apply(header, data)
    assert source.frame is previous


async def test_resize_disable_restart_and_subscriber_cleanup() -> None:
    source = FrameSource("inst_example")
    scanout(source)
    original = source.frame
    source.apply({"kind": "disable"}, b"")
    assert source.frame is None
    scanout(source, width=2, height=4)
    assert source.frame and original and source.frame.seq > original.seq
    assert (source.frame.width, source.frame.height) == (2, 4)
    queue = source.subscribe()
    source.close()
    assert await queue.get() is None
    with pytest.raises(ValueError):
        scanout(source)
    replacement = FrameSource(source.instance_id)
    assert replacement.generation != source.generation


async def test_shadow_snapshot_encodes_pixels_without_an_authoritative_frame_id() -> None:
    source = FrameSource("inst_example")
    scanout(source, value=255)
    result = await source.shadow_capture(CaptureConfig())
    assert result.format_used == "dbus-shadow"
    assert not hasattr(result, "frame_id")
    with Image.open(io.BytesIO(result.png_bytes)) as image:
        assert image.size == (3, 2)
        assert image.getpixel((1, 1)) == (255, 255, 255)
    assert HostConfig().experimental_display == "off"


async def test_bridge_framing_survives_byte_fragmentation_and_rejects_lengths() -> None:
    header = json.dumps({"kind": "scanout", "width": 1, "height": 1}).encode()
    wire = struct.pack(">II", 4 + len(header) + 4, len(header)) + header + b"1234"
    reader = asyncio.StreamReader()
    for byte in wire:
        reader.feed_data(bytes([byte]))
    parsed, data = await read_bridge_packet(reader)
    assert parsed["width"] == 1 and data == b"1234"
    reader = asyncio.StreamReader()
    reader.feed_data(struct.pack(">I", 0xFFFFFFFF))
    with pytest.raises(ValueError):
        await read_bridge_packet(reader)


def test_cursor_metadata_is_retained_between_shape_and_position_updates() -> None:
    source = FrameSource("inst_example")
    scanout(source)
    source.apply({"kind": "cursor", "rgba": "abcd", "width": 1, "height": 1}, b"")
    source.apply({"kind": "cursor", "x": 2, "y": 3, "visible": True}, b"")
    assert source.frame and source.frame.cursor
    assert source.frame.cursor["rgba"] == "abcd"
    assert source.frame.cursor["x"] == 2
    with pytest.raises(TypeError):
        source.frame.cursor["x"] = 99
