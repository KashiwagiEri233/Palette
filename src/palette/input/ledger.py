"""Conservative record of inputs that may still be held by the guest."""

from __future__ import annotations

import contextlib
from typing import Any


class InputLedger:
    """What the service believes is currently held down.

    A press that we failed to release is remembered deliberately: the plan requires the
    cleanup path to know about a possibly-stuck key or button, and requires the service
    to admit when it cannot confirm a release rather than claiming success.
    """

    __slots__ = ("buttons", "keys")

    def __init__(self) -> None:
        self.keys: list[str] = []
        self.buttons: list[str] = []

    def press_key(self, qcode: str) -> None:
        if qcode not in self.keys:
            self.keys.append(qcode)

    def release_key(self, qcode: str) -> None:
        with contextlib.suppress(ValueError):
            self.keys.remove(qcode)

    def press_button(self, button: str) -> None:
        if button not in self.buttons:
            self.buttons.append(button)

    def release_button(self, button: str) -> None:
        with contextlib.suppress(ValueError):
            self.buttons.remove(button)

    @property
    def is_clean(self) -> bool:
        return not self.keys and not self.buttons

    def snapshot(self) -> dict[str, list[str]]:
        return {"keys": list(self.keys), "buttons": list(self.buttons)}

    def note_presses(self, events: list[dict[str, Any]]) -> None:
        """Record a press before it is sent.

        Under-reporting a held key is worse than over-reporting one: the cleanup path only
        releases what it knows about, so an optimistically recorded press is safe and a
        missed one is not.
        """
        for event in events:
            data = event.get("data") or {}
            if event["type"] == "key":
                qcode = (data.get("key") or {}).get("data")
                if isinstance(qcode, str) and data.get("down"):
                    self.press_key(qcode)
            elif event["type"] == "btn":
                button = data.get("button")
                if isinstance(button, str) and not button.startswith("wheel") and data.get("down"):
                    self.press_button(button)

    def note_releases(self, events: list[dict[str, Any]]) -> None:
        """Apply acknowledged transitions in order, releasing only after success.

        A real-time batch can contain up/down or down/up/down for the same input.
        Reapply presses as well so an earlier release cannot erase a later hold.
        """
        for event in events:
            data = event.get("data") or {}
            if event["type"] == "key":
                qcode = (data.get("key") or {}).get("data")
                if isinstance(qcode, str):
                    if data.get("down"):
                        self.press_key(qcode)
                    else:
                        self.release_key(qcode)
            elif event["type"] == "btn":
                button = data.get("button")
                if isinstance(button, str) and not button.startswith("wheel"):
                    if data.get("down"):
                        self.press_button(button)
                    else:
                        self.release_button(button)
