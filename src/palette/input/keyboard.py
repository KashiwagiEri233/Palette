"""Key names, ASCII text planning and chord decomposition.

The central fact this module exists to encode: **a virtual keyboard emits key events, it
does not "type" Unicode.** Text reaches a guest because the guest's layout and input
method interpret a key sequence, so:

* the API speaks in explicit key names (``CTRL``, ``ENTER``, ``A``), which map onto
  QEMU's ``QKeyCode`` enumeration — never onto host scan codes or raw characters;
* strict text mode covers US-layout printable ASCII only, and the *entire* string is
  validated before a single key event is produced, so an unsupported character aborts
  the action rather than typing half of it;
* control characters are refused outright, because a newline is an application-specific
  submit action, not harmless text.

The qcode names below are taken from ``QKeyCode`` in QEMU's ``qapi/ui.json``.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

from ..errors import ErrorCode, PaletteError

# --------------------------------------------------------------------------------------
# qcode tables
# --------------------------------------------------------------------------------------

#: Every QKeyCode value the API is willing to expose. Anything outside this set cannot
#: be named by an agent, so an unsupported key fails during validation instead of
#: silently doing nothing inside the guest.
QCODES: frozenset[str] = frozenset(
    {
        "unmapped",
        "shift",
        "shift_r",
        "alt",
        "alt_r",
        "ctrl",
        "ctrl_r",
        "menu",
        "esc",
        "1",
        "2",
        "3",
        "4",
        "5",
        "6",
        "7",
        "8",
        "9",
        "0",
        "minus",
        "equal",
        "backspace",
        "tab",
        "q",
        "w",
        "e",
        "r",
        "t",
        "y",
        "u",
        "i",
        "o",
        "p",
        "bracket_left",
        "bracket_right",
        "ret",
        "a",
        "s",
        "d",
        "f",
        "g",
        "h",
        "j",
        "k",
        "l",
        "semicolon",
        "apostrophe",
        "grave_accent",
        "backslash",
        "z",
        "x",
        "c",
        "v",
        "b",
        "n",
        "m",
        "comma",
        "dot",
        "slash",
        "asterisk",
        "spc",
        "caps_lock",
        "f1",
        "f2",
        "f3",
        "f4",
        "f5",
        "f6",
        "f7",
        "f8",
        "f9",
        "f10",
        "f11",
        "f12",
        "f13",
        "f14",
        "f15",
        "f16",
        "f17",
        "f18",
        "f19",
        "f20",
        "f21",
        "f22",
        "f23",
        "f24",
        "num_lock",
        "scroll_lock",
        "kp_divide",
        "kp_multiply",
        "kp_subtract",
        "kp_add",
        "kp_enter",
        "kp_decimal",
        "kp_0",
        "kp_1",
        "kp_2",
        "kp_3",
        "kp_4",
        "kp_5",
        "kp_6",
        "kp_7",
        "kp_8",
        "kp_9",
        "kp_comma",
        "kp_equals",
        "print",
        "home",
        "pgup",
        "pgdn",
        "end",
        "left",
        "up",
        "down",
        "right",
        "insert",
        "delete",
        "meta_l",
        "meta_r",
        "pause",
    }
)

#: Modifiers are held down for the duration of a chord and released in reverse order.
MODIFIER_QCODES: frozenset[str] = frozenset(
    {"ctrl", "ctrl_r", "shift", "shift_r", "alt", "alt_r", "meta_l", "meta_r"}
)

#: Friendly API names accepted from agents. The values are qcodes.
KEY_ALIASES: dict[str, str] = {
    "CTRL": "ctrl",
    "CONTROL": "ctrl",
    "CTRL_R": "ctrl_r",
    "SHIFT": "shift",
    "SHIFT_R": "shift_r",
    "ALT": "alt",
    "ALT_R": "alt_r",
    "ALTGR": "alt_r",
    "META": "meta_l",
    "SUPER": "meta_l",
    "WIN": "meta_l",
    "CMD": "meta_l",
    "COMMAND": "meta_l",
    "META_R": "meta_r",
    "ENTER": "ret",
    "RETURN": "ret",
    "ESC": "esc",
    "ESCAPE": "esc",
    "TAB": "tab",
    "SPACE": "spc",
    "SPACEBAR": "spc",
    "BACKSPACE": "backspace",
    "BKSP": "backspace",
    "DELETE": "delete",
    "DEL": "delete",
    "INSERT": "insert",
    "INS": "insert",
    "HOME": "home",
    "END": "end",
    "PAGEUP": "pgup",
    "PGUP": "pgup",
    "PAGEDOWN": "pgdn",
    "PGDN": "pgdn",
    "UP": "up",
    "DOWN": "down",
    "LEFT": "left",
    "RIGHT": "right",
    "CAPSLOCK": "caps_lock",
    "CAPS": "caps_lock",
    "NUMLOCK": "num_lock",
    "SCROLLLOCK": "scroll_lock",
    "PRINTSCREEN": "print",
    "PRTSC": "print",
    "MENU": "menu",
    "PAUSE": "pause",
    "MINUS": "minus",
    "EQUALS": "equal",
    "EQUAL": "equal",
    "COMMA": "comma",
    "PERIOD": "dot",
    "DOT": "dot",
    "SLASH": "slash",
    "BACKSLASH": "backslash",
    "SEMICOLON": "semicolon",
    "APOSTROPHE": "apostrophe",
    "GRAVE": "grave_accent",
    "BACKTICK": "grave_accent",
    "LBRACKET": "bracket_left",
    "RBRACKET": "bracket_right",
    "ASTERISK": "asterisk",
}
for _digit in range(10):
    KEY_ALIASES[f"NUM{_digit}"] = f"kp_{_digit}"
    KEY_ALIASES[f"KP_{_digit}"] = f"kp_{_digit}"
KEY_ALIASES["NUMPAD_ENTER"] = "kp_enter"
KEY_ALIASES["KP_ENTER"] = "kp_enter"

#: Second-chance lookup with underscores removed, so ``page-up``, ``PAGEDOWN``,
#: ``bracketleft`` and ``capslock`` all resolve without an exhaustive alias table.
_LOOSE_ALIASES: dict[str, str] = {key.replace("_", ""): value for key, value in KEY_ALIASES.items()}
_LOOSE_QCODES: dict[str, str] = {qcode.replace("_", ""): qcode for qcode in QCODES}

#: US layout: printable ASCII character -> (qcode, needs_shift).
_ASCII_TABLE: dict[str, tuple[str, bool]] = {}
for _letter in "abcdefghijklmnopqrstuvwxyz":
    _ASCII_TABLE[_letter] = (_letter, False)
    _ASCII_TABLE[_letter.upper()] = (_letter, True)
for _ascii_digit in "1234567890":
    _ASCII_TABLE[_ascii_digit] = (_ascii_digit, False)
_ASCII_TABLE.update(
    {
        " ": ("spc", False),
        "!": ("1", True),
        "@": ("2", True),
        "#": ("3", True),
        "$": ("4", True),
        "%": ("5", True),
        "^": ("6", True),
        "&": ("7", True),
        "*": ("8", True),
        "(": ("9", True),
        ")": ("0", True),
        "-": ("minus", False),
        "_": ("minus", True),
        "=": ("equal", False),
        "+": ("equal", True),
        "[": ("bracket_left", False),
        "{": ("bracket_left", True),
        "]": ("bracket_right", False),
        "}": ("bracket_right", True),
        "\\": ("backslash", False),
        "|": ("backslash", True),
        ";": ("semicolon", False),
        ":": ("semicolon", True),
        "'": ("apostrophe", False),
        '"': ("apostrophe", True),
        "`": ("grave_accent", False),
        "~": ("grave_accent", True),
        ",": ("comma", False),
        "<": ("comma", True),
        ".": ("dot", False),
        ">": ("dot", True),
        "/": ("slash", False),
        "?": ("slash", True),
    }
)

#: Characters the caller must express as an explicit key action instead.
CONTROL_CHARACTERS = {
    "\n": "newline (ENTER)",
    "\r": "carriage return (ENTER)",
    "\t": "tab (TAB)",
    "\x1b": "escape (ESC)",
    "\x08": "backspace (BACKSPACE)",
    "\x7f": "delete (DELETE)",
}


# --------------------------------------------------------------------------------------
# plans
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class KeyEvent:
    """One ``key`` input event."""

    qcode: str
    down: bool

    def to_wire(self) -> dict[str, object]:
        return {
            "type": "key",
            "data": {"key": {"type": "qcode", "data": self.qcode}, "down": self.down},
        }


@dataclass(frozen=True, slots=True)
class KeyPlan:
    """A complete, pre-validated key sequence.

    Building the whole list up front is what makes "validate the entire string before
    emitting anything" true rather than aspirational.
    """

    events: list[KeyEvent]
    description: str
    estimated_ms: int = 0

    @property
    def qcodes(self) -> list[str]:
        return [event.qcode for event in self.events if event.down]


def normalise_key(name: str) -> str:
    """Map an API key name onto a qcode, or fail with a clear reason."""
    if not isinstance(name, str) or not name.strip():
        raise PaletteError(ErrorCode.INVALID_ARGUMENT, "key names must be non-empty strings")
    token = name.strip().upper().replace(" ", "").replace("-", "_")
    if token in KEY_ALIASES:
        return KEY_ALIASES[token]
    lowered = token.lower()
    if lowered in QCODES:
        return lowered
    loose = token.replace("_", "")
    if loose in _LOOSE_ALIASES:
        return _LOOSE_ALIASES[loose]
    if loose.lower() in _LOOSE_QCODES:
        return _LOOSE_QCODES[loose.lower()]
    # A bare letter or digit is the key of that name. Punctuation is deliberately not
    # accepted here: "!" would have to mean pressing 1 with Shift, which is typing, not
    # a key name — that belongs to type_text where the whole string is validated.
    if len(token) == 1 and token.isalnum():
        entry = _ASCII_TABLE.get(token.lower())
        if entry is not None:
            return entry[0]
    raise PaletteError(
        ErrorCode.UNSUPPORTED_CAPABILITY,
        f"unknown key name {name!r}",
        details={"hint": "use an explicit key name such as CTRL, ENTER, TAB, ESC or A"},
    )


def plan_chord(keys: Iterable[str], *, hold_ms: int = 50) -> KeyPlan:
    """Modifiers down, main keys down/up, modifiers released in reverse order.

    Duplicate modifiers are collapsed: ``["CTRL", "CTRL", "A"]`` is a client mistake, not
    a request to press Ctrl twice, and the schema already rejects exact duplicates.
    """
    qcodes = [normalise_key(key) for key in keys]
    if not qcodes:
        raise PaletteError(ErrorCode.INVALID_ARGUMENT, "a key action needs at least one key")

    modifiers: list[str] = []
    mains: list[str] = []
    for qcode in qcodes:
        if qcode in MODIFIER_QCODES:
            if qcode in modifiers:
                continue
            modifiers.append(qcode)
        else:
            mains.append(qcode)
    if not mains and not modifiers:
        raise PaletteError(ErrorCode.INVALID_ARGUMENT, "no usable keys in chord")

    events: list[KeyEvent] = [KeyEvent(qcode, True) for qcode in modifiers]
    events.extend(KeyEvent(qcode, True) for qcode in mains)
    events.extend(KeyEvent(qcode, False) for qcode in reversed(mains))
    events.extend(KeyEvent(qcode, False) for qcode in reversed(modifiers))
    description = "+".join(modifiers + mains) if modifiers else "+".join(mains)
    return KeyPlan(events=events, description=description, estimated_ms=hold_ms)


def plan_text(text: str, *, max_chars: int, strict: bool = True) -> KeyPlan:
    """Plan a strict text entry as key events.

    Raises ``UNSUPPORTED_TEXT`` describing *every* offending character before producing
    any event, so the caller can decide what to do without having typed a prefix.
    """
    if not isinstance(text, str):
        raise PaletteError(ErrorCode.INVALID_ARGUMENT, "text must be a string")
    if text == "":
        raise PaletteError(ErrorCode.INVALID_ARGUMENT, "text must not be empty")
    if len(text) > max_chars:
        raise PaletteError(
            ErrorCode.INVALID_ARGUMENT,
            f"text exceeds the {max_chars} character limit for one action",
            details={"length": len(text), "max_chars": max_chars},
        )

    unsupported: list[dict[str, object]] = []
    events: list[KeyEvent] = []
    for index, character in enumerate(text):
        if character in CONTROL_CHARACTERS:
            unsupported.append(
                {
                    "index": index,
                    "codepoint": f"U+{ord(character):04X}",
                    "reason": f"control character; send {CONTROL_CHARACTERS[character]} instead",
                }
            )
            continue
        entry = _ASCII_TABLE.get(character)
        if entry is None:
            unsupported.append(
                {
                    "index": index,
                    "codepoint": f"U+{ord(character):04X}",
                    "name": unicodedata.name(character, "unknown"),
                    "reason": "outside US printable ASCII",
                }
            )
            continue
        qcode, shift = entry
        if shift:
            events.append(KeyEvent("shift", True))
        events.append(KeyEvent(qcode, True))
        events.append(KeyEvent(qcode, False))
        if shift:
            events.append(KeyEvent("shift", False))

    if unsupported:
        raise PaletteError(
            ErrorCode.UNSUPPORTED_TEXT,
            "the text contains characters this mode cannot type; nothing was sent",
            details={
                "mode": "keyboard",
                "unsupported": unsupported[:8],
                "unsupported_count": len(unsupported),
                "supported": "US-layout printable ASCII",
            },
        )
    return KeyPlan(events=events, description=f"text[{len(text)} chars]")


def select_shift_side(qcode: str, *, right: bool = False) -> str:
    """Allow callers to pick the right-hand modifier deliberately."""
    if qcode != "shift":
        return qcode
    return "shift_r" if right else "shift"
