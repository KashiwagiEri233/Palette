"""Wire models: strictness of the action union, and the frame wire form."""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from palette.errors import ErrorCode, PaletteError
from palette.models import (
    ClickAction,
    Frame,
    ObserveResult,
    Point,
    Receipt,
    Size,
    Viewport,
    parse_action,
)


def code_of(payload: dict) -> ErrorCode:
    with pytest.raises(PaletteError) as info:
        parse_action(payload)
    return info.value.code


class TestActionUnion:
    def test_every_documented_action_parses(self) -> None:
        payloads = [
            {"type": "move", "x": 1, "y": 2},
            {"type": "click", "x": 1, "y": 2},
            {"type": "click", "x": 1, "y": 2, "button": "right", "count": 2},
            {"type": "scroll", "x": 1, "y": 2, "direction": "down", "ticks": 3},
            {
                "type": "drag",
                "points": [{"x": 0, "y": 0}, {"x": 10, "y": 10}],
                "button": "left",
                "duration_ms": 300,
            },
            {"type": "key", "keys": ["CTRL", "A"], "hold_ms": 50},
            {"type": "type_text", "text": "hello", "mode": "keyboard"},
        ]
        for payload in payloads:
            assert parse_action(payload).type == payload["type"]

    def test_an_unknown_action_type_is_reported_as_unsupported(self) -> None:
        assert code_of({"type": "swipe", "x": 1, "y": 2}) is ErrorCode.UNSUPPORTED_CAPABILITY

    def test_unknown_fields_are_rejected(self) -> None:
        assert (
            code_of({"type": "click", "x": 1, "y": 2, "pressure": 0.5})
            is ErrorCode.INVALID_ARGUMENT
        )

    def test_negative_coordinates_are_rejected_before_the_controller_sees_them(self) -> None:
        assert code_of({"type": "move", "x": -1, "y": 2}) is ErrorCode.INVALID_ARGUMENT

    @pytest.mark.parametrize("count", [0, 3, 10])
    def test_click_counts_are_limited_to_one_or_two(self, count: int) -> None:
        assert (
            code_of({"type": "click", "x": 1, "y": 2, "count": count}) is ErrorCode.INVALID_ARGUMENT
        )

    @pytest.mark.parametrize("ticks", [0, 21, 1000])
    def test_scroll_is_bounded(self, ticks: int) -> None:
        payload = {"type": "scroll", "x": 1, "y": 2, "direction": "up", "ticks": ticks}
        assert code_of(payload) is ErrorCode.INVALID_ARGUMENT

    def test_scroll_direction_is_closed(self) -> None:
        payload = {"type": "scroll", "x": 1, "y": 2, "direction": "left", "ticks": 1}
        assert code_of(payload) is ErrorCode.INVALID_ARGUMENT

    def test_drag_needs_at_least_two_points(self) -> None:
        payload = {"type": "drag", "points": [{"x": 0, "y": 0}]}
        assert code_of(payload) is ErrorCode.INVALID_ARGUMENT

    def test_drag_is_capped_at_sixty_four_points(self) -> None:
        payload = {"type": "drag", "points": [{"x": i, "y": i} for i in range(65)]}
        assert code_of(payload) is ErrorCode.INVALID_ARGUMENT

    def test_drag_must_actually_move(self) -> None:
        payload = {"type": "drag", "points": [{"x": 5, "y": 5}, {"x": 5, "y": 5}]}
        assert code_of(payload) is ErrorCode.INVALID_ARGUMENT

    def test_drag_duration_is_bounded_to_three_seconds(self) -> None:
        payload = {
            "type": "drag",
            "points": [{"x": 0, "y": 0}, {"x": 9, "y": 9}],
            "duration_ms": 9000,
        }
        assert code_of(payload) is ErrorCode.INVALID_ARGUMENT

    def test_chords_are_capped_at_six_keys(self) -> None:
        payload = {"type": "key", "keys": ["CTRL", "SHIFT", "ALT", "A", "B", "C", "D"]}
        assert code_of(payload) is ErrorCode.INVALID_ARGUMENT

    @pytest.mark.parametrize("hold", [0, 9, 501, 5000])
    def test_hold_time_is_bounded(self, hold: int) -> None:
        assert (
            code_of({"type": "key", "keys": ["A"], "hold_ms": hold}) is ErrorCode.INVALID_ARGUMENT
        )

    def test_key_names_are_normalised_to_upper_case(self) -> None:
        action = parse_action({"type": "key", "keys": ["ctrl", "a"]})
        assert action.keys == ["CTRL", "A"]  # type: ignore[union-attr]

    def test_duplicate_keys_in_a_chord_are_rejected(self) -> None:
        assert code_of({"type": "key", "keys": ["CTRL", "CTRL"]}) is ErrorCode.INVALID_ARGUMENT

    def test_text_mode_is_closed(self) -> None:
        assert (
            code_of({"type": "type_text", "text": "hi", "mode": "unicode"})
            is ErrorCode.INVALID_ARGUMENT
        )

    def test_empty_text_is_rejected(self) -> None:
        assert code_of({"type": "type_text", "text": ""}) is ErrorCode.INVALID_ARGUMENT

    def test_no_raw_press_or_release_surface_exists(self) -> None:
        """There is deliberately no way to express "hold this key until later"."""
        for payload in (
            {"type": "key_down", "key": "CTRL"},
            {"type": "mouse_down", "button": "left"},
            {"type": "raw", "events": []},
        ):
            assert code_of(payload) is ErrorCode.UNSUPPORTED_CAPABILITY

    def test_non_finite_numbers_cannot_be_expressed(self) -> None:
        for value in (float("nan"), float("inf")):
            assert code_of({"type": "move", "x": value, "y": 0}) is ErrorCode.INVALID_ARGUMENT

    def test_literal_values_are_pinned(self) -> None:
        with pytest.raises(ValidationError):
            ClickAction(type="click", x=1, y=1, button="wheel-up")  # type: ignore[arg-type]

    def test_point_requires_non_negative_integers(self) -> None:
        with pytest.raises(ValidationError):
            Point(x=-1, y=0)


class TestFrame:
    def make_frame(self, **overrides) -> Frame:
        base = {
            "frame_id": "frame_1",
            "vm_id": "desktop",
            "instance_id": "inst_1",
            "boot_epoch": 1,
            "display_id": "video0",
            "head": 0,
            "display_epoch": 1,
            "controller_epoch": 1,
            "action_seq": 0,
            "native_size": Size(width=1280, height=800),
            "image_size": Size(width=640, height=400),
            "viewport": Viewport(x=0, y=0, width=1280, height=800),
            "transform_id": "fit_640x400_of_1280x800",
            "captured_at": datetime.now().astimezone(),
            "monotonic_stamp": 123.0,
        }
        base.update(overrides)
        return Frame(**base)

    def test_the_monotonic_stamp_never_leaves_the_host(self) -> None:
        wire = self.make_frame().to_wire()
        assert "monotonic_stamp" not in wire

    def test_the_wire_form_revalidates(self) -> None:
        """A frame rebuilt from structuredContent must parse; it has no stamp, so it is
        treated as expired rather than silently trusted."""
        wire = self.make_frame().to_wire()
        rebuilt = Frame.model_validate(wire)
        assert rebuilt.monotonic_stamp == 0.0
        assert rebuilt.frame_id == "frame_1"

    def test_captured_at_keeps_its_utc_offset(self) -> None:
        wire = self.make_frame().to_wire()
        assert wire["captured_at"].endswith(("+00:00", "+08:00")) or "T" in wire["captured_at"]

    def test_frame_ids_are_unique_and_unguessable(self) -> None:
        ids = {Frame.new_id() for _ in range(200)}
        assert len(ids) == 200
        assert all(value.startswith("frame_") and len(value) > 20 for value in ids)

    def test_cursor_source_cannot_claim_an_observation_it_did_not_make(self) -> None:
        with pytest.raises(ValidationError):
            self.make_frame(cursor_position_source="observed")


class TestReceiptAndResults:
    def test_a_successful_observation_must_carry_a_frame(self) -> None:
        with pytest.raises(ValidationError):
            ObserveResult(ok=True)

    def test_a_failed_observation_must_carry_an_error(self) -> None:
        with pytest.raises(ValidationError):
            ObserveResult(ok=False)

    def test_effect_states_are_the_documented_four(self) -> None:
        for state in ("not_sent", "submitted", "partial", "unknown"):
            receipt = Receipt(ok=True, request_id="r", effect_state=state)  # type: ignore[arg-type]
            assert receipt.effect_state == state

    def test_receipts_serialise_without_nulls(self) -> None:
        receipt = Receipt(ok=True, request_id="r", effect_state="submitted")
        wire = receipt.to_wire()
        assert wire["ok"] is True
        assert "frame" not in wire
        assert "error" not in wire
