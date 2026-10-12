"""Error codes and the exception type carried across every Palette layer.

The codes are the ones listed in the plan's error model. They exist so that an agent
client can branch on a stable identifier instead of matching on prose, and so that the
audit log can record a result without recording the payload that produced it.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """Stable, machine-readable failure identifiers."""

    # --- lifecycle / infrastructure ---
    VM_NOT_FOUND = "VM_NOT_FOUND"
    VM_NOT_RUNNING = "VM_NOT_RUNNING"
    VM_ALREADY_RUNNING = "VM_ALREADY_RUNNING"
    STATE_CONFLICT = "STATE_CONFLICT"
    QEMU_NOT_FOUND = "QEMU_NOT_FOUND"
    ACCELERATION_UNAVAILABLE = "ACCELERATION_UNAVAILABLE"
    IMAGE_MISSING = "IMAGE_MISSING"
    IMAGE_INVALID = "IMAGE_INVALID"
    PRECONDITION_FAILED = "PRECONDITION_FAILED"
    STARTUP_FAILED = "STARTUP_FAILED"
    QMP_UNAVAILABLE = "QMP_UNAVAILABLE"
    QMP_COMMAND_FAILED = "QMP_COMMAND_FAILED"

    # --- observation ---
    CAPTURE_FAILED = "CAPTURE_FAILED"
    CAPTURE_TOO_LARGE = "CAPTURE_TOO_LARGE"
    FRAME_NOT_FOUND = "FRAME_NOT_FOUND"
    STALE_FRAME = "STALE_FRAME"

    # --- actions ---
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    INVALID_COORDINATES = "INVALID_COORDINATES"
    UNSUPPORTED_TEXT = "UNSUPPORTED_TEXT"
    UNSUPPORTED_CAPABILITY = "UNSUPPORTED_CAPABILITY"
    CONTROL_NOT_OWNED = "CONTROL_NOT_OWNED"
    BUSY = "BUSY"
    RATE_LIMITED = "RATE_LIMITED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    ACTION_OUTCOME_UNKNOWN = "ACTION_OUTCOME_UNKNOWN"
    CAPTURE_FAILED_AFTER_INPUT = "CAPTURE_FAILED_AFTER_INPUT"
    REQUEST_ID_CONFLICT = "REQUEST_ID_CONFLICT"

    # --- authorization / policy ---
    POLICY_DENIED = "POLICY_DENIED"
    PATH_DENIED = "PATH_DENIED"
    OPERATOR_REQUIRED = "OPERATOR_REQUIRED"

    # --- guest enhancement channel ---
    GUEST_TOOL_UNAVAILABLE = "GUEST_TOOL_UNAVAILABLE"
    GUEST_TOOL_VERSION_MISMATCH = "GUEST_TOOL_VERSION_MISMATCH"
    APP_NOT_ALLOWED = "APP_NOT_ALLOWED"
    INPUT_TARGET_UNSUPPORTED = "INPUT_TARGET_UNSUPPORTED"
    FOCUS_CHANGED = "FOCUS_CHANGED"

    # --- internal ---
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


#: Codes for which an immediate, unchanged retry may plausibly succeed.
RETRYABLE_CODES: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.BUSY,
        ErrorCode.RATE_LIMITED,
        ErrorCode.TIMEOUT,
    }
)

#: Codes that describe a failure whose side effects may or may not have happened.
#: The plan forbids automatic replay for these, so they are marked explicitly.
UNKNOWN_OUTCOME_CODES: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.ACTION_OUTCOME_UNKNOWN,
        ErrorCode.CAPTURE_FAILED_AFTER_INPUT,
    }
)


class PaletteError(Exception):
    """A failure with a stable code, safe for an agent to read.

    ``message`` never contains host paths, tokens or process environments; sensitive
    detail belongs in the audit log, not in a tool result.
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.retryable = RETRYABLE_CODES.__contains__(code) if retryable is None else retryable

    def as_payload(self) -> dict[str, Any]:
        """Serialise to the structured error body returned to the agent."""
        payload: dict[str, Any] = {
            "code": str(self.code),
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.details:
            payload["details"] = self.details
        return payload

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"PaletteError({self.code!s}, {self.message!r})"


class QmpCommandError(PaletteError):
    """A QMP command returned an ``error`` message."""

    def __init__(self, qmp_class: str, description: str, command: str | None = None) -> None:
        super().__init__(
            ErrorCode.QMP_COMMAND_FAILED,
            f"QMP command {'?' if command is None else command} failed: {description}",
            details={"qmp_class": qmp_class, "command": command},
        )
        self.qmp_class = qmp_class
        self.description = description
        self.command = command


class QmpDisconnectedError(PaletteError):
    """The QMP channel reached EOF or its pipe broke."""

    def __init__(self, message: str = "QMP channel is not available") -> None:
        super().__init__(ErrorCode.QMP_UNAVAILABLE, message)
