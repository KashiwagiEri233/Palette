"""Identity shared by the VM owner and its input transaction controller."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from ..models import ErrorPayload, GuestToolResult, Receipt, Size

CursorPositionSource = Literal["not_observed", "last_command"]
RequestKey = tuple[str, int, int, str]
ActionResult = Receipt | GuestToolResult


@dataclass(slots=True)
class InstanceView:
    """The mutable identity of one QEMU process, shared by the manager and controller.

    Kept as a plain object so that both sides mutate the same epochs; anything derived
    from an epoch is therefore invalidated by construction rather than by remembering to
    call an invalidation helper in the right order.
    """

    vm_id: str
    instance_id: str
    profile_id: str
    display_id: str
    head: int
    boot_epoch: int = 1
    display_epoch: int = 1
    controller_epoch: int = 1
    action_seq: int = 0
    native_size: Size | None = None
    state: str = "STOPPED"
    qmp_ready: bool = False
    screen_ready: bool = False
    desktop_ready: bool | None = None
    started_at: datetime | None = None
    last_error: ErrorPayload | None = None
