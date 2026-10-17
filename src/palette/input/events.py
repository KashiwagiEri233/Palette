"""Plan ordered QMP input batches without sending or mutating runtime state."""

from __future__ import annotations

from typing import Any

from ..config import Profile
from ..errors import ErrorCode, PaletteError
from ..models import (
    Action,
    ClickAction,
    DragAction,
    KeyAction,
    MoveAction,
    ScrollAction,
    TypeTextAction,
)
from .coordinates import CoordinateTransform, interpolate
from .keyboard import plan_chord, plan_text


class InputPlanner:
    def __init__(self, profile: Profile) -> None:
        self.profile = profile

    def batches(
        self, action: Action, transform: CoordinateTransform
    ) -> list[tuple[list[dict[str, Any]], int]]:
        """Turn an action into ordered ``input-send-event`` batches.

        One phase per message: a press and its release are never merged into a single
        request, because HID polling, event synchronisation and guest scheduling all sit
        between them and a merged message makes the timing unobservable.
        """
        batches: list[tuple[list[dict[str, Any]], int]] = []

        if isinstance(action, MoveAction):
            batches.append((absolute_events(*transform.to_qemu(action.x, action.y)), 0))
        elif isinstance(action, ClickAction):
            batches.extend(self._click_batches(action, transform))
        elif isinstance(action, ScrollAction):
            batches.append((absolute_events(*transform.to_qemu(action.x, action.y)), 0))
            button = "wheel-up" if action.direction == "up" else "wheel-down"
            for _ in range(action.ticks):
                batches.append(([button_event(button, True)], 8))
                batches.append(([button_event(button, False)], 8))
        elif isinstance(action, DragAction):
            batches.extend(self._drag_batches(action, transform))
        elif isinstance(action, KeyAction):
            plan = plan_chord(action.keys, hold_ms=action.hold_ms)
            batches.extend(key_plan_batches(plan, hold_ms=action.hold_ms))
        elif isinstance(action, TypeTextAction):
            plan = plan_text(action.text, max_chars=self.profile.text.max_chars)
            interval = self.profile.text.char_interval_ms
            batches.extend(key_plan_batches(plan, hold_ms=interval, gap_ms=interval))
            budget_ms = self.profile.text.max_total_ms
            if sum(delay for _events, delay in batches) > budget_ms:
                raise PaletteError(
                    ErrorCode.RATE_LIMITED,
                    "this text would exceed the configured per-action typing budget",
                    details={"budget_ms": budget_ms},
                )
        else:  # pragma: no cover - the action union is closed
            raise PaletteError(ErrorCode.UNSUPPORTED_CAPABILITY, "unsupported action type")
        return batches

    def _click_batches(
        self, action: ClickAction, transform: CoordinateTransform
    ) -> list[tuple[list[dict[str, Any]], int]]:
        timeouts = self.profile.timeouts
        batches: list[tuple[list[dict[str, Any]], int]] = [
            (absolute_events(*transform.to_qemu(action.x, action.y)), 0)
        ]
        for index in range(action.count):
            batches.append(([button_event(action.button, True)], timeouts.click_hold_ms))
            batches.append(([button_event(action.button, False)], 0))
            if index < action.count - 1:
                batches.append(([], timeouts.double_click_gap_ms))
        return [batch for batch in batches if batch[0] or batch[1]]

    def _drag_batches(
        self, action: DragAction, transform: CoordinateTransform
    ) -> list[tuple[list[dict[str, Any]], int]]:
        # More samples for longer paths, but bounded by the schema's 64 point limit.
        steps = max(2, min(6, 48 // max(1, len(action.points) - 1)))
        path = interpolate([(point.x, point.y) for point in action.points], steps_per_segment=steps)
        per_step_ms = int(action.duration_ms / max(1, len(path) - 1)) if action.duration_ms else 0
        batches: list[tuple[list[dict[str, Any]], int]] = [
            (absolute_events(*transform.to_qemu(*path[0])), 0),
            ([button_event(action.button, True)], per_step_ms),
        ]
        for point in path[1:]:
            batches.append((absolute_events(*transform.to_qemu(*point)), per_step_ms))
        batches.append(([button_event(action.button, False)], 0))
        return batches


def absolute_events(qx: int, qy: int) -> list[dict[str, Any]]:
    # X and Y travel together so the pointer never appears at an intermediate corner.
    return [
        {"type": "abs", "data": {"axis": "x", "value": qx}},
        {"type": "abs", "data": {"axis": "y", "value": qy}},
    ]


def button_event(button: str, down: bool) -> dict[str, Any]:
    return {"type": "btn", "data": {"button": button, "down": down}}


def key_event(qcode: str, down: bool) -> dict[str, Any]:
    return {"type": "key", "data": {"key": {"type": "qcode", "data": qcode}, "down": down}}


def key_plan_batches(
    plan: Any, *, hold_ms: int, gap_ms: int = 0
) -> list[tuple[list[dict[str, Any]], int]]:
    """One message per key event, with a hold between down and up.

    Modifier release order is preserved from the plan (which already reverses it), so the
    wiring here stays a straightforward translation.
    """
    batches: list[tuple[list[dict[str, Any]], int]] = []
    events = list(plan.events)
    for index, event in enumerate(events):
        delay = 0
        if event.down:
            next_down_is_other = index + 1 < len(events) and events[index + 1].down
            delay = 0 if next_down_is_other else hold_ms
        else:
            delay = gap_ms
        batches.append(([key_event(event.qcode, event.down)], delay))
    return batches
