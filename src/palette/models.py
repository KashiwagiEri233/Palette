"""Wire models: frames, actions, receipts.

Everything crossing the MCP boundary is described here. Two rules shape these types:

* **Unknown fields are rejected.** ``extra="forbid"`` everywhere, so a client cannot
  smuggle an unsupported parameter past validation and hope a backend honours it.
* **The action union is closed.** Six action types, no raw press/release surface, so
  there is no way to express "hold this key until further notice".
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .errors import ErrorCode, PaletteError

# --------------------------------------------------------------------------------------
# geometry
# --------------------------------------------------------------------------------------

#: QEMU absolute pointer axis range for ``usb-tablet``.
QEMU_ABS_MAX = 32767

#: Declared in the abs event schema: the guest tablet reports in this space.
CURSOR_POSITION_SOURCES = ("not_observed", "last_command")


class Size(BaseModel):
    """A pixel extent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    width: int = Field(gt=0)
    height: int = Field(gt=0)

    def as_dict(self) -> dict[str, int]:
        return {"width": self.width, "height": self.height}


class Point(BaseModel):
    """An image-space pixel coordinate: origin top-left, X right, Y down."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    x: int = Field(ge=0)
    y: int = Field(ge=0)


class Viewport(BaseModel):
    """The sub-rectangle of the framebuffer a frame covers. The MVP always uses the
    whole surface, but the field exists so that a future crop cannot silently reuse
    the uncropped transform."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    x: int = Field(ge=0)
    y: int = Field(ge=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)

    @classmethod
    def full(cls, size: Size) -> Self:
        return cls(x=0, y=0, width=size.width, height=size.height)


# --------------------------------------------------------------------------------------
# frames
# --------------------------------------------------------------------------------------


class Frame(BaseModel):
    """Everything needed to decide whether an action derived from an image is still
    applicable, and how to map the coordinates back to the framebuffer.

    ``frame_id`` is an identifier, not an authorization: it is unguessable so that it
    cannot be forged, but possession of one still requires an owning session, VM and
    matching epochs.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    frame_id: str
    vm_id: str
    instance_id: str
    boot_epoch: int = Field(ge=0)
    display_id: str
    head: int = Field(ge=0)
    display_epoch: int = Field(ge=0)
    controller_epoch: int = Field(ge=0)
    action_seq: int = Field(ge=0)
    native_size: Size = Field(description="Framebuffer size as reported by the capture backend")
    image_size: Size = Field(description="Size of the image the agent actually received")
    viewport: Viewport
    transform_id: str
    captured_at: datetime
    monotonic_stamp: float = Field(
        default=0.0,
        exclude=True,
        description=(
            "Host monotonic clock reading used for expiry; never sent to the agent. It is "
            "excluded from the wire form, so a frame rebuilt from structuredContent has a "
            "zero stamp and is treated as expired rather than silently trusted."
        ),
    )
    cursor_position_source: Literal["not_observed", "last_command"] = "not_observed"

    @classmethod
    def new_id(cls) -> str:
        return f"frame_{uuid.uuid4().hex}"

    def to_wire(self) -> dict[str, Any]:
        """Serialise for ``structuredContent`` (monotonic stamp excluded)."""
        return self.model_dump(mode="json", exclude={"monotonic_stamp"})


# --------------------------------------------------------------------------------------
# actions
# --------------------------------------------------------------------------------------

Button = Literal["left", "middle", "right"]
ScrollDirection = Literal["up", "down"]


class _ActionBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MoveAction(_ActionBase):
    """Set the absolute pointer position."""

    type: Literal["move"]
    x: int = Field(ge=0)
    y: int = Field(ge=0)


class ClickAction(_ActionBase):
    """Move to the target, press, hold briefly, release."""

    type: Literal["click"]
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    button: Button = "left"
    count: Literal[1, 2] = 1


class ScrollAction(_ActionBase):
    """Discrete vertical wheel pulses at a position. Horizontal and smooth scrolling
    are deliberately not expressible."""

    type: Literal["scroll"]
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    direction: ScrollDirection
    ticks: int = Field(ge=1, le=20)


class DragAction(_ActionBase):
    """Press at the first point, travel through the intermediates, release at the last."""

    type: Literal["drag"]
    points: list[Point] = Field(min_length=2, max_length=64)
    button: Button = "left"
    duration_ms: int = Field(default=400, ge=0, le=3000)

    @model_validator(mode="after")
    def _no_duplicate_point_run(self) -> Self:
        if all(p == self.points[0] for p in self.points):
            raise ValueError("drag needs at least two distinct points")
        return self


class KeyAction(_ActionBase):
    """A chord, or a single key. Modifiers are pressed first and released in reverse."""

    type: Literal["key"]
    keys: list[str] = Field(min_length=1, max_length=6)
    hold_ms: int = Field(default=50, ge=10, le=500)

    @field_validator("keys")
    @classmethod
    def _keys_are_names(cls, value: list[str]) -> list[str]:
        cleaned = [key.strip().upper() for key in value]
        if any(not key for key in cleaned):
            raise ValueError("empty key name")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("duplicate key in chord")
        return cleaned


class TypeTextAction(_ActionBase):
    """Strict keyboard text: a sequence of key events, never a Unicode write.

    The whole string is validated before any key event is emitted, so an unsupported
    character aborts the action instead of typing half of it.
    """

    type: Literal["type_text"]
    text: str = Field(min_length=1)
    mode: Literal["keyboard"] = "keyboard"


Action = Annotated[
    MoveAction | ClickAction | ScrollAction | DragAction | KeyAction | TypeTextAction,
    Field(discriminator="type"),
]

ACTION_TYPES = ("move", "click", "scroll", "drag", "key", "type_text")

#: Actions that can move the pointer, change focus or otherwise alter desktop state.
#: They advance ``action_seq`` and therefore fence every frame captured before them.
STATE_CHANGING_ACTION_TYPES = frozenset(ACTION_TYPES)


def parse_action(payload: dict[str, Any]) -> Action:
    """Validate an action body, converting pydantic's errors into a Palette error."""
    from pydantic import TypeAdapter, ValidationError

    try:
        return TypeAdapter(Action).validate_python(payload)
    except ValidationError as exc:
        unsupported = any(err.get("type") == "union_tag_invalid" for err in exc.errors())
        code = ErrorCode.UNSUPPORTED_CAPABILITY if unsupported else ErrorCode.INVALID_ARGUMENT
        raise PaletteError(
            code,
            f"unsupported or invalid action: {_summarise_validation_error(exc)}",
            details={"action_type": payload.get("type") if isinstance(payload, dict) else None},
        ) from exc


def _summarise_validation_error(exc: Any) -> str:
    parts = []
    for err in exc.errors()[:4]:
        location = ".".join(str(item) for item in err.get("loc", ())) or "<action>"
        parts.append(f"{location}: {err.get('msg', 'invalid')}")
    return "; ".join(parts)


# --------------------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------------------


class EffectState(StrEnum):
    """How far an action got, from the service's point of view.

    ``submitted`` says the input reached QEMU, not that a guest application reacted.
    ``unknown`` forbids automatic replay.
    """

    NOT_SENT = "not_sent"
    SUBMITTED = "submitted"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


class ActionSource(StrEnum):
    QMP = "qmp"
    GUEST_TOOL = "guest_tool"


class ObservationState(StrEnum):
    CAPTURED = "captured"
    FAILED = "failed"
    SKIPPED = "skipped"


class ErrorPayload(BaseModel):
    """The structured error body. Never carries host paths or input text."""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] | None = None


class Receipt(BaseModel):
    """The result of one action transaction."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    request_id: str
    action_type: str | None = None
    effect_state: EffectState
    action_seq: int = 0
    source: ActionSource = ActionSource.QMP
    backend: str | None = Field(
        default=None,
        description="The concrete backend that ran, e.g. 'qmp-keyboard', 'native_text'",
    )
    launch_id: str | None = None
    deduplicated: bool = False
    input_submitted: bool = False
    observation: ObservationState = ObservationState.SKIPPED
    frame: Frame | None = None
    frame_observation_required: bool = False
    error: ErrorPayload | None = None
    elapsed_ms: int = 0

    def to_wire(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


class DisplayInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_id: str
    head: int
    width: int
    height: int


class InputLedgerState(BaseModel):
    """What the service believes it is still holding down."""

    model_config = ConfigDict(extra="forbid")

    keys: list[str] = Field(default_factory=list)
    buttons: list[str] = Field(default_factory=list)
    release_confirmed: bool = True


class GuestToolStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    transport_online: bool = False
    service_online: bool = False
    interactive_desktop_ready: bool | None = None
    protocol_version: str | None = None
    connection_generation: int = 0
    capabilities: list[str] = Field(default_factory=list)
    input_methods: list[str] = Field(default_factory=list)
    allowed_apps: list[str] = Field(default_factory=list)
    detail: str | None = None


class VmStatus(BaseModel):
    """The ``vm_status`` body: lifecycle and readiness are reported separately."""

    model_config = ConfigDict(extra="forbid")

    vm_id: str
    profile_id: str | None = None
    instance_id: str | None = None
    state: str
    host_arch: str | None = None
    guest_arch: str | None = None
    accelerator: str | None = None
    cross_architecture: bool = False
    process_protection: str | None = None
    memory_enforcement: str | None = None
    memory_sample_interval_ms: int | None = None
    boot_epoch: int = 0
    display_epoch: int = 0
    controller_epoch: int = 0
    action_seq: int = 0
    qmp_ready: bool = False
    screen_ready: bool = False
    desktop_ready: bool | None = Field(
        default=None,
        description="None means 'not determined'; readiness is never inferred from query-status",
    )
    display: DisplayInfo | None = None
    input_ledger: InputLedgerState = Field(default_factory=InputLedgerState)
    guest_tools: GuestToolStatus = Field(default_factory=GuestToolStatus)
    queued_actions: int = 0
    controller_owned: bool = True
    control_owner: Literal["agent", "operator"] = "agent"
    control_state: Literal["available", "held", "releasing", "faulted"] = "available"
    frame_ttl_seconds: float = 0.0
    last_error: ErrorPayload | None = None
    uptime_seconds: float | None = None
    started_at: datetime | None = None


class ObserveResult(BaseModel):
    """Result of ``vm_observe`` and of every post-action capture.

    One schema covers both outcomes: a failure carries ``ok=false`` with a structured
    ``error`` and no frame, which is more useful to a client than a second, separately
    advertised error shape.
    """

    model_config = ConfigDict(extra="forbid")

    ok: bool
    error: ErrorPayload | None = None
    frame: Frame | None = None
    image_mime_type: str = "image/png"
    image_bytes: int = 0
    scaled: bool = False
    note: str | None = None

    @model_validator(mode="after")
    def _frame_iff_ok(self) -> Self:
        if self.ok and self.frame is None:
            raise ValueError("a successful observation must carry a frame")
        if not self.ok and self.error is None:
            raise ValueError("a failed observation must carry a structured error")
        return self


class WaitReason(StrEnum):
    DELAY_ELAPSED = "delay_elapsed"
    CONTENT_CHANGED = "content_changed"
    CONTENT_STABLE = "content_stable"
    TIMEOUT = "timeout"


class WaitResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    error: ErrorPayload | None = None
    reason: WaitReason | None = None
    elapsed_ms: int = 0
    samples: int = 0
    changed_ratio: float | None = None
    frame: Frame | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _reason_and_frame_iff_ok(self) -> Self:
        if self.ok and (self.reason is None or self.frame is None):
            raise ValueError("a completed wait must carry a reason and a frame")
        if not self.ok and self.error is None:
            raise ValueError("a failed wait must carry a structured error")
        return self


class ReleaseResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    error: ErrorPayload | None = None
    released_keys: list[str] = Field(default_factory=list)
    released_buttons: list[str] = Field(default_factory=list)
    release_confirmed: bool = True
    note: str | None = None

    @model_validator(mode="after")
    def _failure_has_error(self) -> Self:
        if not self.ok and self.error is None and self.note is None:
            raise ValueError("a failed release must explain itself")
        return self


class LifecycleResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    error: ErrorPayload | None = None
    vm_id: str
    state: str = "UNKNOWN"
    instance_id: str | None = None
    profile_id: str | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _failure_has_error(self) -> Self:
        if not self.ok and self.error is None:
            raise ValueError("a failed lifecycle operation must carry a structured error")
        return self


class VmSummary(BaseModel):
    """Agent discovery metadata, without operator configuration or host paths."""

    model_config = ConfigDict(extra="forbid")

    vm_id: str
    name: str
    persistent: bool
    state: str
    guest_arch: str | None = None
    instance_id: str | None = None
    screen_ready: bool = False
    control_owner: Literal["agent", "operator"] = "agent"
    control_state: Literal["available", "held", "releasing", "faulted"] = "available"
    agent_driver_claimed: bool = False


class VmListResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    error: ErrorPayload | None = None
    vms: list[VmSummary] = Field(default_factory=list)


class StatusResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    error: ErrorPayload | None = None
    status: VmStatus | None = None
    capabilities: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _status_iff_ok(self) -> Self:
        if self.ok and self.status is None:
            raise ValueError("a successful status must carry the VM status")
        return self


class GuestToolResult(BaseModel):
    """Result of the retained guest enhancement tools (§7.6).

    Shares the ``effect_state`` vocabulary with the QMP path and adds ``source`` so a
    receipt can never be mistaken for a visual action.
    """

    model_config = ConfigDict(extra="forbid")

    ok: bool
    error: ErrorPayload | None = None
    source: Literal["guest_tool"] = "guest_tool"
    effect_state: EffectState = EffectState.UNKNOWN
    method: str | None = None
    launch_id: str | None = None
    request_id: str | None = None
    action_seq: int = 0
    deduplicated: bool = False
    frame: Frame | None = None
    detail: str | None = None

    @model_validator(mode="after")
    def _failure_has_error(self) -> Self:
        if not self.ok and self.error is None:
            raise ValueError("a failed guest tool call must carry a structured error")
        return self
