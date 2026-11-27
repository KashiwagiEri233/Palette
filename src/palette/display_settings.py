"""Operator-owned local display preferences, independent of the raw-frame experiment."""

from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from .errors import ErrorCode, PaletteError
from .policy import PathGuard

logger = logging.getLogger("palette.display")
DisplayMode = Literal["inherit", "low_latency", "compatibility"]


class _SavedSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1] = 1
    mode: Literal["low_latency", "compatibility"]


class DisplaySettings:
    """An absent preference preserves trusted configuration; explicit modes override it."""

    def __init__(self, runtime_root: Path) -> None:
        self.guard = PathGuard(runtime_root)
        self.mode: DisplayMode = "inherit"
        self.reason: str | None = None
        try:
            path = self.guard.resolve("display-settings.json")
            if path.is_file():
                with path.open("rb") as stream:
                    payload = stream.read(4097)
                if len(payload) > 4096:
                    raise ValueError("oversized display settings")
                self.mode = _SavedSettings.model_validate_json(payload).mode
        except (OSError, ValueError, ValidationError, PaletteError):
            # Corrupt local preferences never accidentally turn on an experimental path.
            self.mode = "compatibility"
            self.reason = "Saved display settings are invalid; compatibility mode is active"
            logger.warning(self.reason)

    def update(self, payload: dict[str, object]) -> None:
        if (
            set(payload) != {"mode"}
            or not isinstance(payload["mode"], str)
            or payload["mode"] not in {"low_latency", "compatibility"}
        ):
            raise PaletteError(ErrorCode.INVALID_ARGUMENT, "invalid display setting")
        saved = _SavedSettings.model_validate({"mode": payload["mode"]})
        target = self.guard.resolve("display-settings.json")
        temporary = self.guard.resolve(f"display-settings-{uuid.uuid4().hex}.tmp")
        try:
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(saved.model_dump(), stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        except OSError as error:
            raise PaletteError(
                ErrorCode.INTERNAL_ERROR, "display settings could not be saved"
            ) from error
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                logger.warning("could not remove a temporary display settings file")
        self.mode = saved.mode
        self.reason = None
        # replace is the commit point: a directory fsync failure must not leave memory
        # describing the old value while the next runtime would read the new one.
        try:
            _sync_directory(target.parent)
        except OSError:
            self.reason = (
                "Display settings were saved, but power-loss durability could not be confirmed"
            )
            logger.warning(self.reason)


def _sync_directory(path: Path) -> None:
    if os.name != "nt":
        from .definitions import sync_directory

        sync_directory(path)
