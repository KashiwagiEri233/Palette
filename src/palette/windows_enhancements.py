"""Closed, bounded arguments for the Windows interactive guest worker."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .errors import ErrorCode, PaletteError


class WindowsParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class FileSystemParameters(WindowsParameters):
    mode: Literal["read", "write", "copy", "move", "delete", "list", "search", "info"]
    path: str = Field(min_length=1, max_length=4096)
    destination: str | None = Field(default=None, max_length=4096)
    content: str | None = Field(default=None, max_length=49152)
    append: bool = False
    recursive: bool = False
    overwrite: bool = False
    pattern: str | None = Field(default=None, max_length=256)
    offset: int = Field(default=1, ge=1)
    limit: int = Field(default=200, ge=1, le=2000)
    encoding: Literal["utf-8", "utf-16", "ascii"] = "utf-8"

    @model_validator(mode="after")
    def required_fields(self) -> FileSystemParameters:
        if self.mode in {"copy", "move"} and not self.destination:
            raise ValueError("destination is required")
        if self.mode == "write" and self.content is None:
            raise ValueError("content is required")
        if self.mode == "search" and not self.pattern:
            raise ValueError("pattern is required")
        if self.content is not None and len(self.content.encode("utf-8")) > 49152:
            raise ValueError("text exceeds 48 KiB")
        return self


class PowerShellParameters(WindowsParameters):
    command: str = Field(min_length=1, max_length=32768)
    timeout_seconds: float = Field(default=20, ge=1, le=60)
    cwd: str | None = Field(default=None, max_length=4096)


class ProcessParameters(WindowsParameters):
    mode: Literal["list", "kill"]
    name: str | None = Field(default=None, max_length=256)
    pid: int | None = Field(default=None, gt=0)
    sort_by: Literal["memory", "cpu", "name"] = "memory"
    limit: int = Field(default=20, ge=1, le=2000)
    force: bool = False

    @model_validator(mode="after")
    def target(self) -> ProcessParameters:
        if self.mode == "kill" and (self.pid is None) == (not self.name):
            raise ValueError("kill requires exactly one of pid or name")
        return self


class RegistryParameters(WindowsParameters):
    mode: Literal["get", "set", "delete", "list"]
    path: str = Field(min_length=1, max_length=4096)
    name: str | None = Field(default=None, max_length=16383)
    value: str | int | list[str] | list[int] | None = None
    type: Literal["String", "ExpandString", "Binary", "DWord", "MultiString", "QWord"] = "String"

    @model_validator(mode="after")
    def typed_value(self) -> RegistryParameters:
        if self.mode in {"get", "set"} and self.name is None:
            raise ValueError("name is required; use an empty name for the default value")
        if self.mode != "set":
            return self
        value = self.value
        valid = False
        if self.type in {"String", "ExpandString"}:
            valid = isinstance(value, str) and len(value.encode("utf-8")) <= 49152
        elif self.type in {"DWord", "QWord"}:
            numeric = value
            if (
                self.type == "QWord"
                and isinstance(value, str)
                and value.isascii()
                and value.isdecimal()
                and len(value) <= 20
            ):
                numeric = int(value)
            valid = type(numeric) is int and 0 <= numeric < 2 ** (
                32 if self.type == "DWord" else 64
            )
        elif isinstance(value, list) and len(value) <= 4096:
            valid = (
                all(type(item) is int and 0 <= item <= 255 for item in value)
                if self.type == "Binary"
                else all(isinstance(item, str) for item in value)
                and len("".join(str(item) for item in value).encode("utf-8")) <= 49152
            )
        if not valid:
            raise ValueError("value does not match the registry type or exceeds its limit")
        return self


class AppParameters(WindowsParameters):
    mode: Literal["launch", "launch_executable", "resize", "switch"] = "launch"
    name: str | None = Field(default=None, max_length=4096)
    executable: str | None = Field(default=None, max_length=4096)
    args: list[str] = Field(default_factory=list, max_length=64)
    cwd: str | None = Field(default=None, max_length=4096)
    window_id: str | None = Field(default=None, max_length=128)
    window_loc: list[int] | None = Field(default=None, min_length=2, max_length=2)
    window_size: list[int] | None = Field(default=None, min_length=2, max_length=2)

    @model_validator(mode="after")
    def mode_parameters(self) -> AppParameters:
        if self.mode == "launch" and not self.name:
            raise ValueError("launch requires a Start Menu name")
        if self.mode == "launch_executable" and not self.executable:
            raise ValueError("launch_executable requires an executable")
        if self.mode != "launch_executable" and (self.executable or self.args or self.cwd):
            raise ValueError("executable, args and cwd require launch_executable")
        if self.mode == "launch_executable" and self.name:
            raise ValueError("name and executable are separate launch modes")
        if self.mode in {"launch", "launch_executable"} and (
            self.window_id or self.window_loc or self.window_size
        ):
            raise ValueError("window arguments require a window mode")
        if self.mode == "switch" and (self.window_loc or self.window_size):
            raise ValueError("window geometry requires resize")
        if self.window_size and any(size <= 0 or size > 32768 for size in self.window_size):
            raise ValueError("invalid window size")
        if sum(len(item.encode("utf-8")) for item in self.args) > 49152:
            raise ValueError("argv exceeds 48 KiB")
        if any("\0" in item or len(item) > 4096 for item in self.args):
            raise ValueError("invalid executable argument")
        return self


class SnapshotParameters(WindowsParameters):
    max_elements: int = Field(default=500, ge=1, le=2000)


class BatchParameters(WindowsParameters):
    snapshot_id: str | None = Field(default=None, min_length=1, max_length=128)
    locs: list[list[Any]] | None = Field(default=None, min_length=1, max_length=64)
    labels: list[Any] | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def one_target_kind(self) -> BatchParameters:
        if (self.locs is None) == (self.labels is None):
            raise ValueError("provide exactly one of locs or labels")
        if self.labels is not None and not self.snapshot_id:
            raise ValueError("labels require snapshot_id")
        if self.locs is not None and self.snapshot_id is not None:
            raise ValueError("snapshot_id is only used with labels")
        return self


class MultiSelectParameters(BatchParameters):
    press_ctrl: bool = True


class MultiEditParameters(BatchParameters):
    clear: bool = True


WINDOWS_MODELS: dict[str, type[WindowsParameters]] = {
    "filesystem": FileSystemParameters,
    "powershell": PowerShellParameters,
    "process": ProcessParameters,
    "registry": RegistryParameters,
    "app": AppParameters,
    "snapshot": SnapshotParameters,
    "multi_select": MultiSelectParameters,
    "multi_edit": MultiEditParameters,
}


def validate_windows(operation: str, values: dict[str, Any]) -> WindowsParameters:
    try:
        request = WINDOWS_MODELS[operation].model_validate(values)
        if isinstance(request, BatchParameters):
            editing = operation == "multi_edit"
            text_bytes = 0
            for entry in request.locs if request.locs is not None else request.labels or []:
                if request.locs is not None:
                    if not isinstance(entry, list) or len(entry) != (3 if editing else 2):
                        raise ValueError("invalid coordinate entry")
                    if any(type(value) is not int or value < 0 for value in entry[:2]):
                        raise ValueError("coordinates must be nonnegative integers")
                elif editing:
                    if not isinstance(entry, list) or len(entry) != 2 or type(entry[0]) is not int:
                        raise ValueError("invalid label entry")
                    if entry[0] < 0:
                        raise ValueError("invalid label")
                elif type(entry) is not int or entry < 0:
                    raise ValueError("invalid label")
                if editing:
                    if not isinstance(entry, list):
                        raise ValueError("invalid edit entry")
                    text = entry[-1]
                    if not isinstance(text, str) or "\0" in text:
                        raise ValueError("invalid field text")
                    text_bytes += len(text.encode("utf-8"))
            if text_bytes > 49152:
                raise ValueError("batch text exceeds 48 KiB")
        # NUL is not a valid Win32 path, argument or text payload.
        for value in request.model_dump().values():
            if isinstance(value, str) and "\0" in value:
                raise ValueError("NUL is not supported")
        return request
    except (ValidationError, ValueError, UnicodeError) as exc:
        raise PaletteError(ErrorCode.INVALID_ARGUMENT, "invalid Windows guest parameters") from exc
