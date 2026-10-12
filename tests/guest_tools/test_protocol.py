"""Guest tool protocol: framing, version negotiation and message validation."""

from __future__ import annotations

import json

import pytest

from palette.errors import ErrorCode, PaletteError
from palette.guest_tools.protocol import (
    PROTOCOL_VERSION,
    Envelope,
    FrameDecoder,
    MessageKind,
    encode_frame,
    negotiate_version,
    parse_envelope,
)


class TestFraming:
    def test_a_frame_round_trips(self) -> None:
        payload = {"kind": "hello", "id": "1", "payload": {"a": 1}}
        decoder = FrameDecoder()
        messages = decoder.feed(encode_frame(payload))
        assert messages == [payload]
        assert decoder.buffered_bytes == 0

    def test_a_half_frame_is_held_until_the_rest_arrives(self) -> None:
        encoded = encode_frame({"kind": "hello", "id": "1"})
        decoder = FrameDecoder()
        assert decoder.feed(encoded[:3]) == []
        assert decoder.buffered_bytes == 3
        messages = decoder.feed(encoded[3:])
        assert len(messages) == 1

    def test_several_frames_in_one_read_are_all_returned(self) -> None:
        decoder = FrameDecoder()
        blob = encode_frame({"kind": "a", "id": "1"}) + encode_frame({"kind": "b", "id": "2"})
        messages = decoder.feed(blob)
        assert [message["kind"] for message in messages] == ["a", "b"]

    def test_a_frame_split_at_every_boundary_still_parses(self) -> None:
        encoded = encode_frame({"kind": "hello", "id": "1", "payload": {"x": "y"}})
        for split in range(1, len(encoded)):
            decoder = FrameDecoder()
            assert decoder.feed(encoded[:split]) == []
            assert len(decoder.feed(encoded[split:])) == 1

    def test_an_oversized_frame_is_refused_rather_than_allocated(self) -> None:
        """The length prefix is attacker-controlled only in the sense that a compromised
        guest controls it; the host must not allocate whatever it asks for."""
        decoder = FrameDecoder(max_bytes=1024)
        import struct

        hostile = struct.pack(">I", 50_000_000) + b"x"
        with pytest.raises(PaletteError) as info:
            decoder.feed(hostile)
        assert info.value.code is ErrorCode.INVALID_ARGUMENT
        assert decoder.discarded == 1

    def test_an_encoding_a_message_cannot_exceed_the_limit(self) -> None:
        from palette.errors import PaletteError as Error

        with pytest.raises(Error):
            encode_frame({"kind": "x", "id": "1", "payload": {"blob": "y" * 4096}}, max_bytes=1024)

    def test_unparseable_bodies_are_discarded_and_the_stream_continues(self) -> None:
        import struct

        decoder = FrameDecoder()
        garbage = struct.pack(">I", 3) + b"\xff\xfe\xfd"
        valid = encode_frame({"kind": "hello", "id": "1"})
        messages = decoder.feed(garbage + valid)
        assert [message["kind"] for message in messages] == ["hello"]
        assert decoder.discarded == 1

    def test_a_json_non_object_is_discarded(self) -> None:
        import struct

        decoder = FrameDecoder()
        body = json.dumps([1, 2, 3]).encode()
        assert decoder.feed(struct.pack(">I", len(body)) + body) == []
        assert decoder.discarded == 1


class TestEnvelope:
    def test_unknown_fields_are_rejected(self) -> None:
        with pytest.raises(PaletteError) as info:
            parse_envelope({"v": "1.0", "kind": "hello", "id": "1", "surprise": True})
        assert info.value.code is ErrorCode.GUEST_TOOL_UNAVAILABLE

    def test_a_missing_id_is_rejected(self) -> None:
        with pytest.raises(PaletteError):
            parse_envelope({"v": "1.0", "kind": "hello"})

    def test_a_well_formed_envelope_parses(self) -> None:
        envelope = parse_envelope(
            {"v": PROTOCOL_VERSION, "kind": MessageKind.HELLO.value, "id": "1", "payload": {}}
        )
        assert isinstance(envelope, Envelope)
        assert envelope.kind == "hello"


class TestVersionNegotiation:
    def test_the_highest_common_version_wins(self) -> None:
        assert negotiate_version(["1.0", "1.1", "2.0"], ["1.0", "1.1"]) == "1.1"

    def test_no_common_version_is_a_reported_mismatch(self) -> None:
        with pytest.raises(PaletteError) as info:
            negotiate_version(["1.0"], ["2.0"])
        assert info.value.code is ErrorCode.GUEST_TOOL_VERSION_MISMATCH
        assert info.value.retryable is False
        assert info.value.details["host"] == ["1.0"]

    def test_a_single_shared_version_is_enough(self) -> None:
        assert negotiate_version(["1.0"], ["1.0"]) == "1.0"
