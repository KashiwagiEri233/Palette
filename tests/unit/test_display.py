"""RFB filtering must hold across arbitrary packet fragmentation."""

import struct

import pytest

from palette.display import RfbFilter, vnc_response


def test_rfb_split_and_combined_packets() -> None:
    parser = RfbFilter()
    encodings = b"\x02\0" + struct.pack(">Hii", 2, 0, -258)
    refresh = b"\x03\x01" + struct.pack(">HHHH", 0, 0, 320, 200)
    output = b"".join(parser.feed(bytes([byte])) for byte in encodings + refresh)
    assert output == b"\x02\0\0\x01\0\0\0\0" + refresh


@pytest.mark.parametrize("kind", [4, 5, 6, 250, 251, 255, 99])
def test_rfb_rejects_writes_even_after_valid_message(kind: int) -> None:
    parser = RfbFilter()
    with pytest.raises(ValueError):
        parser.feed(b"\x03\x01" + b"\0" * 8 + bytes([kind]))


def test_rfb_rejects_unsolicited_extensions_and_oversized_encoding_list() -> None:
    with pytest.raises(ValueError):
        RfbFilter().feed(b"\x02\0" + struct.pack(">H", 257))
    with pytest.raises(ValueError):
        RfbFilter().feed(b"\x96\x01" + b"\0" * 8)


def test_vnc_auth_vector() -> None:
    assert vnc_response("password", bytes(range(16))).hex() == "b866924125c8eebb9debc1db61c538e2"


def test_rfb_strips_heavy_compression_and_prefers_copyrect_and_raw() -> None:
    parser = RfbFilter()
    offered = [7, -260, 16, 21, 6, 5, 1, 0, -223, -239, -312, -313, -26, -254]
    packet = b"\x02\0" + struct.pack(f">H{len(offered)}i", len(offered), *offered)
    filtered = parser.feed(packet)
    count = struct.unpack_from(">H", filtered, 2)[0]
    selected = list(struct.unpack(f">{count}i", filtered[4:]))
    assert selected == [1, 0, 5, -223, -239, -312, -313]


def test_rfb_falls_back_to_raw_when_only_compressed_encodings_offered() -> None:
    parser = RfbFilter()
    offered = [7, 16, 21]
    packet = b"\x02\0" + struct.pack(f">H{len(offered)}i", len(offered), *offered)
    filtered = parser.feed(packet)
    count = struct.unpack_from(">H", filtered, 2)[0]
    selected = list(struct.unpack(f">{count}i", filtered[4:]))
    assert selected == [0]
