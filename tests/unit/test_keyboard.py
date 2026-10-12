"""Key names, chords and strict text planning."""

from __future__ import annotations

import pytest

from palette.errors import ErrorCode, PaletteError
from palette.input.keyboard import (
    MODIFIER_QCODES,
    QCODES,
    normalise_key,
    plan_chord,
    plan_text,
)


class TestKeyNames:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("CTRL", "ctrl"),
            ("control", "ctrl"),
            ("ENTER", "ret"),
            ("return", "ret"),
            ("ESC", "esc"),
            ("ESCAPE", "esc"),
            ("TAB", "tab"),
            ("SPACE", "spc"),
            ("PGUP", "pgup"),
            ("PAGEDOWN", "pgdn"),
            ("BACKSPACE", "backspace"),
            ("DELETE", "delete"),
            ("A", "a"),
            ("z", "z"),
            ("5", "5"),
            ("F12", "f12"),
            ("WIN", "meta_l"),
            ("num3", "kp_3"),
            ("page-up", "pgup"),
        ],
    )
    def test_aliases_resolve_to_qcodes(self, name: str, expected: str) -> None:
        assert normalise_key(name) == expected

    def test_every_resolved_qcode_is_valid(self) -> None:
        for name in ("CTRL", "SHIFT", "ALT", "META", "ENTER", "F5", "ASTERISK"):
            assert normalise_key(name) in QCODES

    def test_unknown_names_are_refused(self) -> None:
        with pytest.raises(PaletteError) as info:
            normalise_key("HYPER")
        assert info.value.code is ErrorCode.UNSUPPORTED_CAPABILITY

    @pytest.mark.parametrize("name", ["!", "@", "/", ""])
    def test_punctuation_is_not_a_key_name(self, name: str) -> None:
        """A shifted character is typing, not a key name: '!' means "press 1 with Shift",
        which belongs to type_text where the whole string is validated first."""
        with pytest.raises(PaletteError) as info:
            normalise_key(name)
        assert info.value.code in {ErrorCode.UNSUPPORTED_CAPABILITY, ErrorCode.INVALID_ARGUMENT}


class TestChords:
    def test_modifiers_are_released_in_reverse_order(self) -> None:
        plan = plan_chord(["CTRL", "SHIFT", "A"])
        assert [(event.qcode, event.down) for event in plan.events] == [
            ("ctrl", True),
            ("shift", True),
            ("a", True),
            ("a", False),
            ("shift", False),
            ("ctrl", False),
        ]

    def test_a_single_key_is_pressed_and_released(self) -> None:
        plan = plan_chord(["ENTER"])
        assert [(event.qcode, event.down) for event in plan.events] == [
            ("ret", True),
            ("ret", False),
        ]

    def test_duplicate_modifiers_are_collapsed(self) -> None:
        plan = plan_chord(["CTRL", "CTRL", "C"])
        assert [event.qcode for event in plan.events if event.down] == ["ctrl", "c"]

    def test_control_letter_combinations_are_expressible(self) -> None:
        for letter in ("A", "C", "V", "S"):
            plan = plan_chord(["CTRL", letter])
            assert plan.qcodes == ["ctrl", letter.lower()]

    def test_a_bare_modifier_is_expressible(self) -> None:
        plan = plan_chord(["SHIFT"])
        assert [(event.qcode, event.down) for event in plan.events] == [
            ("shift", True),
            ("shift", False),
        ]

    def test_the_wire_form_matches_the_qapi_key_value_union(self) -> None:
        plan = plan_chord(["CTRL"])
        assert plan.events[0].to_wire() == {
            "type": "key",
            "data": {"key": {"type": "qcode", "data": "ctrl"}, "down": True},
        }

    def test_empty_chords_are_refused(self) -> None:
        with pytest.raises(PaletteError) as info:
            plan_chord([])
        assert info.value.code is ErrorCode.INVALID_ARGUMENT

    def test_modifiers_are_a_known_subset(self) -> None:
        assert MODIFIER_QCODES <= QCODES


class TestTextPlanning:
    def test_plain_letters_need_no_modifier(self) -> None:
        plan = plan_text("abc", max_chars=256)
        assert plan.qcodes == ["a", "b", "c"]
        assert not any(event.qcode == "shift" for event in plan.events)

    def test_uppercase_uses_shift(self) -> None:
        plan = plan_text("A", max_chars=256)
        assert [(event.qcode, event.down) for event in plan.events] == [
            ("shift", True),
            ("a", True),
            ("a", False),
            ("shift", False),
        ]

    @pytest.mark.parametrize(
        ("character", "qcode"),
        [
            ("!", "1"),
            ("@", "2"),
            ("#", "3"),
            ("$", "4"),
            ("%", "5"),
            ("^", "6"),
            ("&", "7"),
            ("*", "8"),
            ("(", "9"),
            (")", "0"),
            ("_", "minus"),
            ("+", "equal"),
            ("{", "bracket_left"),
            ("}", "bracket_right"),
            ("|", "backslash"),
            (":", "semicolon"),
            ('"', "apostrophe"),
            ("~", "grave_accent"),
            ("<", "comma"),
            (">", "dot"),
            ("?", "slash"),
        ],
    )
    def test_shifted_symbols_map_to_their_unshifted_key(self, character: str, qcode: str) -> None:
        plan = plan_text(character, max_chars=256)
        assert [event.qcode for event in plan.events if event.down] == ["shift", qcode]

    def test_space_and_unshifted_punctuation_need_no_modifier(self) -> None:
        for character, qcode in ((" ", "spc"), ("-", "minus"), (",", "comma"), ("/", "slash")):
            plan = plan_text(character, max_chars=256)
            assert [event.qcode for event in plan.events if event.down] == [qcode]

    @pytest.mark.parametrize("character", ["\n", "\r", "\t", "\x1b", "\x7f"])
    def test_control_characters_are_refused(self, character: str) -> None:
        with pytest.raises(PaletteError) as info:
            plan_text(f"a{character}b", max_chars=256)
        assert info.value.code is ErrorCode.UNSUPPORTED_TEXT
        assert info.value.details["unsupported"][0]["index"] == 1

    def test_unicode_is_refused_with_the_offending_codepoint(self) -> None:
        with pytest.raises(PaletteError) as info:
            plan_text("hi 你好", max_chars=256)
        assert info.value.code is ErrorCode.UNSUPPORTED_TEXT
        reported = info.value.details["unsupported"]
        assert reported[0]["codepoint"] == "U+4F60"
        assert info.value.details["supported"] == "US-layout printable ASCII"

    def test_nothing_is_planned_when_anything_is_unsupported(self) -> None:
        """The whole string is validated before any key event exists, so a partial send
        cannot happen even in principle."""
        with pytest.raises(PaletteError) as info:
            plan_text("oké", max_chars=256)
        assert info.value.code is ErrorCode.UNSUPPORTED_TEXT
        assert "plan" not in info.value.details

    def test_all_unsupported_characters_are_reported_together(self) -> None:
        with pytest.raises(PaletteError) as info:
            plan_text("一ab二", max_chars=256)
        assert info.value.details["unsupported_count"] == 2

    def test_the_character_limit_is_enforced(self) -> None:
        with pytest.raises(PaletteError) as info:
            plan_text("a" * 257, max_chars=256)
        assert info.value.code is ErrorCode.INVALID_ARGUMENT
        assert info.value.details["max_chars"] == 256

    def test_empty_text_is_refused(self) -> None:
        with pytest.raises(PaletteError) as info:
            plan_text("", max_chars=256)
        assert info.value.code is ErrorCode.INVALID_ARGUMENT

    def test_every_printable_ascii_character_is_typable(self) -> None:
        printable = "".join(chr(code) for code in range(0x20, 0x7F))
        plan = plan_text(printable, max_chars=256)
        assert plan.description == f"text[{len(printable)} chars]"
        assert all(qcode in QCODES for qcode in plan.qcodes if qcode != "shift")

    def test_a_full_sentence_round_trips_through_the_planner(self) -> None:
        text = "Hello, World! 123 (test) [ok] #5 @ 50%"
        plan = plan_text(text, max_chars=256)
        typed = [qcode for qcode in plan.qcodes if qcode != "shift"]
        assert typed[0] == "shift" or typed[0] == "h"
        assert plan.qcodes.count("spc") == text.count(" ")
