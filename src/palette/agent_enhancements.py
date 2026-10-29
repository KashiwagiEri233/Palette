"""Closed agent operations over the standard QEMU guest channels."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .enhancements import enhancement_status
from .errors import ErrorCode, PaletteError
from .guest_tools.base import GuestToolOutcome
from .models import EffectState
from .windows_enhancements import (
    WINDOWS_MODELS,
    PowerShellParameters,
    WindowsParameters,
    validate_windows,
)

if TYPE_CHECKING:
    from .vm.manager import VmInstance


class Parameters(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str = Field(default="", max_length=49152)
    path: str = Field(default="", max_length=4096)
    mode: str = "r"
    transfer_id: str = Field(default="", max_length=128)
    count: int = Field(default=49152, ge=1, le=49152)
    data_base64: str = Field(default="", max_length=65536)
    width: int = Field(default=1024, ge=320, le=8192)
    height: int = Field(default=768, ge=200, le=8192)


FIELDS: dict[str, set[str]] = {
    "clipboard.get": set(),
    "clipboard.set": {"text"},
    "display.resize": {"width", "height"},
    "sync_time": set(),
    "file.open": {"path", "mode"},
    "file.read": {"transfer_id", "count"},
    "file.write": {"transfer_id", "data_base64"},
    "file.close": {"transfer_id"},
}


def validate_parameters(operation: str, values: dict[str, Any]) -> Parameters | WindowsParameters:
    if operation in WINDOWS_MODELS:
        return validate_windows(operation, values)
    if operation not in FIELDS or set(values) - FIELDS[operation]:
        raise PaletteError(ErrorCode.INVALID_ARGUMENT, "unsupported guest operation or parameters")
    try:
        request = Parameters.model_validate(values)
        if operation == "clipboard.set":
            data = request.text.encode("utf-8")
            if len(data) > 49152 or b"\0" in data:
                raise ValueError
        if operation == "file.open":
            path = request.path
            if (
                request.mode not in {"r", "w"}
                or "\0" in path
                or not (
                    path.startswith("/") or (len(path) > 2 and path[1] == ":" and path[2] in "\\/")
                )
            ):
                raise ValueError
        if operation in {"file.read", "file.write", "file.close"} and not request.transfer_id:
            raise ValueError
        if (
            operation == "file.write"
            and not 0 < len(base64.b64decode(request.data_base64, validate=True)) <= 49152
        ):
            raise ValueError
        if operation == "display.resize" and request.width * request.height > 16_777_216:
            raise ValueError
    except (ValidationError, ValueError, UnicodeError, binascii.Error) as exc:
        raise PaletteError(
            ErrorCode.INVALID_ARGUMENT, "invalid guest operation parameters"
        ) from exc
    return request


async def execute(
    instance: VmInstance,
    operation: str,
    request: Parameters | WindowsParameters,
    on_sending: Callable[[], None],
) -> GuestToolOutcome:
    if isinstance(request, WindowsParameters):
        instance.policy.require_capability("guest." + operation)
        backend = instance.guest_tools
        if backend is None:
            raise PaletteError(ErrorCode.GUEST_TOOL_UNAVAILABLE, "Windows guest worker is offline")
        parameters = request.model_dump(exclude_none=True)
        if isinstance(request, PowerShellParameters):
            budget = min(
                instance.profile.guest_tools.request_timeout_seconds - 2,
                instance.profile.resources.max_action_seconds - 3,
            )
            if budget < 1:
                raise PaletteError(
                    ErrorCode.INVALID_ARGUMENT, "profile deadline is too short for PowerShell"
                )
            parameters["timeout_seconds"] = min(request.timeout_seconds, budget)
        return await backend.execute_operation(
            operation=operation,
            parameters=parameters,
            on_sending=on_sending,
        )
    status = enhancement_status(instance)
    desktop = instance.guest_desktop
    data: dict[str, Any]
    if operation.startswith("clipboard.") or operation == "display.resize":
        feature = "resize" if operation == "display.resize" else "clipboard"
        instance.policy.require_capability(f"guest.{feature}")
        if feature == "clipboard" and instance.guest_tools is not None:
            caps = await instance.guest_tools.capabilities()
            if caps.supports("guest.clipboard"):
                if (
                    len(request.text.encode("utf-8"))
                    > instance.profile.guest_desktop.max_clipboard_bytes
                ):
                    raise PaletteError(
                        ErrorCode.INVALID_ARGUMENT, "clipboard text exceeds profile limit"
                    )
                result = await instance.guest_tools.execute_operation(
                    operation=operation,
                    parameters={"text": request.text} if operation == "clipboard.set" else {},
                    on_sending=on_sending,
                )
                if result.ok and operation == "clipboard.get":
                    text = result.data.get("text")
                    if (
                        not isinstance(text, str)
                        or len(text.encode("utf-8"))
                        > instance.profile.guest_desktop.max_clipboard_bytes
                    ):
                        return GuestToolOutcome(
                            ok=False,
                            effect_state=EffectState.NOT_SENT,
                            error=PaletteError(
                                ErrorCode.INVALID_ARGUMENT,
                                "guest clipboard result exceeds profile limit",
                            ),
                        )
                return result
        if not status["desktop"][feature] or desktop is None:
            raise PaletteError(
                ErrorCode.GUEST_TOOL_UNAVAILABLE,
                f"guest {feature} is unavailable; baseline visual input remains available",
            )
        if operation == "clipboard.set" and len(request.text.encode("utf-8")) > desktop.limit:
            raise PaletteError(ErrorCode.INVALID_ARGUMENT, "clipboard text exceeds profile limit")
        on_sending()
        if operation == "clipboard.get":
            data = await desktop.clipboard_get()
        elif operation == "clipboard.set":
            data = await desktop.clipboard_set(request.text)
        else:
            data = await desktop.resize(request.width, request.height)
    else:
        capability = "guest.sync_time" if operation == "sync_time" else "guest.file"
        instance.policy.require_capability(capability)
        agent = instance.qemu_guest_agent
        if agent is None or not agent.online:
            raise PaletteError(ErrorCode.GUEST_TOOL_UNAVAILABLE, "QEMU Guest Agent is unavailable")
        required = {"sync_time": "guest-set-time"}.get(
            operation, "guest-" + operation.replace(".", "-")
        )
        commands = {required}
        if operation == "file.open":
            commands |= {
                "guest-file-close",
                "guest-file-read" if request.mode == "r" else "guest-file-write",
            }
        if not commands <= agent.commands:
            raise PaletteError(
                ErrorCode.UNSUPPORTED_CAPABILITY, "required QGA commands are disabled"
            )
        if operation == "sync_time":
            data = await agent.sync_time(on_sending=on_sending)
        elif operation == "file.open":
            data = await agent.file_open(request.path, request.mode, on_sending=on_sending)
        elif operation == "file.read":
            data = await agent.file_read(request.transfer_id, request.count, on_sending=on_sending)
        elif operation == "file.write":
            data = await agent.file_write(
                request.transfer_id, request.data_base64, on_sending=on_sending
            )
        else:
            data = await agent.file_close(request.transfer_id, on_sending=on_sending)
    return GuestToolOutcome(ok=True, method=operation, data=data)
