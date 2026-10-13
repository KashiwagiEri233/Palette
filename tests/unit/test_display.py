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
