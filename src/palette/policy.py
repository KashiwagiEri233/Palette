"""Authorization, path confinement and capability gating.

Two invariants are enforced here, and nowhere else:

1. **Agent input never becomes a host path or a QEMU argument.** Every filesystem
   location Palette touches is composed from trusted configuration and a
   service-generated identifier, then checked to be inside the managed runtime root.
2. **Power comes from trusted configuration, not from the agent's own claims.** An
   agent cannot grant itself the reset permission by sending ``confirmed=true``.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .config import Profile
from .errors import ErrorCode, PaletteError

#: Characters that must never appear in a service-generated identifier.
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

LifecycleOperation = Literal["start", "stop", "force_stop", "reset"]

_CAPABILITY_LABELS: dict[str, str] = {
    "observe": "vm_observe / vm_status",
    "control": "vm_act / vm_wait / vm_release_inputs",
    "lifecycle.start": "vm_start",
    "lifecycle.stop": "vm_stop",
    "lifecycle.reset": "vm_reset",
    "guest.input_text": "vm_guest_input_text",
    "guest.launch_app": "vm_guest_launch_app",
}

#: The complete set of capabilities this build knows. A name outside it is not a
#: permissions question but an unregistered feature, which is what keeps the guest channel
#: from turning into a general-purpose RPC passthrough: adding a capability means adding it
#: here, to the schema, and to the policy.
REGISTERED_CAPABILITIES: frozenset[str] = frozenset(_CAPABILITY_LABELS)


def validate_identifier(value: str, *, what: str = "identifier") -> str:
    """Reject anything that is not a short, opaque, filesystem-safe token.

    Identifiers are accepted from the agent (``vm_id``) but are *not* treated as
    paths: they are validated against a strict pattern before any path is built.
    """
    if not isinstance(value, str) or not _SAFE_ID.match(value) or value in {".", ".."}:
        raise PaletteError(
            ErrorCode.INVALID_ARGUMENT,
            f"invalid {what}",
            details={"value": value if isinstance(value, str) else type(value).__name__},
        )
    return value


def _is_reparse_point(path: Path) -> bool:
    """Detect Windows junctions and symlinks, which can escape a lexical prefix check."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    attributes = getattr(st, "st_file_attributes", 0)
    if attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
        return True
    return stat.S_ISLNK(st.st_mode)


def _normalise(path: Path) -> str:
    text = str(path)
    return os.path.normcase(os.path.normpath(text))


@dataclass(frozen=True)
class PathGuard:
    """Confines every managed filesystem object to one runtime root.

    Checks are lexical *and* physical: ``resolve()`` collapses ``..`` and follows
    existing links, and reparse points are rejected explicitly so that a junction
    planted inside the runtime root cannot redirect cleanup elsewhere.
    """

    root: Path
    create_root: bool = True

    def __post_init__(self) -> None:
        if self.create_root:
            self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, *parts: str | os.PathLike[str], must_exist: bool = False) -> Path:
        candidate = self.root
        for part in parts:
            text = str(part)
            if os.path.isabs(text) or text.startswith(("/", "\\")):
                raise PaletteError(
                    ErrorCode.PATH_DENIED, "absolute path components are not allowed"
                )
            candidate = candidate / text
        resolved = Path(os.path.abspath(candidate))

        root_text = _normalise(self.root.resolve())
        resolved_text = _normalise(resolved)
        if resolved_text != root_text and not resolved_text.startswith(root_text + os.sep):
            raise PaletteError(
                ErrorCode.PATH_DENIED,
                "path escapes the managed runtime root",
            )

        # Every existing component must be a real directory/file, not a link out.
        probe = self.root.resolve()
        try:
            relative_parts = resolved.resolve().relative_to(probe).parts
        except ValueError:
            raise PaletteError(
                ErrorCode.PATH_DENIED, "path escapes the managed runtime root"
            ) from None
        for part in relative_parts:
            probe = probe / part
            if probe.exists() and _is_reparse_point(probe):
                raise PaletteError(
                    ErrorCode.PATH_DENIED,
                    "refusing to follow a link or reparse point inside the runtime root",
                    details={"component": part},
                )

        if must_exist and not resolved.exists():
            raise PaletteError(ErrorCode.IMAGE_MISSING, "required managed file is missing")
        return resolved

    def contains(self, path: Path) -> bool:
        root_text = _normalise(self.root.resolve())
        path_text = _normalise(Path(os.path.abspath(path)))
        return path_text == root_text or path_text.startswith(root_text + os.sep)

    def assert_managed(self, path: Path) -> Path:
        """Used before any destructive operation on a resolved path."""
        if not self.contains(path):
            raise PaletteError(ErrorCode.PATH_DENIED, "refusing to touch an unmanaged path")
        if _is_reparse_point(path):
            raise PaletteError(ErrorCode.PATH_DENIED, "refusing to touch a link or reparse point")
        return path


class Policy:
    """Per-profile permission checks. One instance per served VM."""

    def __init__(self, profile: Profile, guard: PathGuard) -> None:
        self.profile = profile
        self.guard = guard

    # -- lifecycle ------------------------------------------------------------------
    def require_lifecycle(self, operation: LifecycleOperation, *, actor: str) -> None:
        """Gate start/stop/reset. Agents are refused unless the profile says otherwise."""
        if actor == "operator":
            return
        allowed = {
            "start": self.profile.lifecycle.allow_agent_start,
            "stop": self.profile.lifecycle.allow_agent_stop,
            "force_stop": self.profile.lifecycle.allow_agent_force_stop,
            "reset": self.profile.lifecycle.allow_agent_reset,
        }[operation]
        if not allowed:
            raise PaletteError(
                ErrorCode.POLICY_DENIED,
                f"profile {self.profile.profile_id!r} does not grant agents the "
                f"{operation!r} lifecycle operation",
                details={"operation": operation, "actor": actor},
                retryable=False,
            )

    # -- capabilities ---------------------------------------------------------------
    def require_capability(self, name: str) -> None:
        """Gate one capability.

        The registry check comes first: an unregistered capability is a missing feature,
        not a permission the profile can grant, and reporting it as "disabled" would
        suggest that a configuration change could turn it on.
        """
        if name not in REGISTERED_CAPABILITIES:
            raise PaletteError(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                f"capability {name!r} is not registered in this build",
                details={"registered": sorted(REGISTERED_CAPABILITIES)},
            )
        if not name.startswith("guest."):
            # Non-guest capabilities are enforced where they are used: observation and
            # control are always available, lifecycle is gated by require_lifecycle.
            return
        if not self.profile.guest_tools.enabled:
            raise PaletteError(
                ErrorCode.GUEST_TOOL_UNAVAILABLE,
                "guest enhancement tools are disabled in this profile",
                details={"capability": name},
            )
        if name not in self.profile.guest_tools.capabilities:
            raise PaletteError(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                f"capability {name!r} is not enabled for this profile",
                details={"capability": name},
            )

    def capabilities(self) -> list[str]:
        """The capability list reported by ``vm_status``."""
        # Observation and control are per-tool policy decisions, not profile switches:
        # they are available whenever the profile is served. Lifecycle stays operator-only.
        available = ["observe", "control"]
        for operation, flag in (
            ("lifecycle.start", self.profile.lifecycle.allow_agent_start),
            ("lifecycle.stop", self.profile.lifecycle.allow_agent_stop),
            ("lifecycle.reset", self.profile.lifecycle.allow_agent_reset),
        ):
            if flag:
                available.append(operation)
        if self.profile.guest_tools.enabled:
            available.extend(sorted(self.profile.guest_tools.capabilities))
        return available

    def describe_capabilities(self) -> dict[str, str]:
        return {name: _CAPABILITY_LABELS.get(name, name) for name in self.capabilities()}

    # -- app registry ---------------------------------------------------------------
    def require_app(self, app_id: str) -> dict[str, object]:
        registry = self.profile.guest_tools.allowed_apps
        if app_id not in registry:
            raise PaletteError(
                ErrorCode.APP_NOT_ALLOWED,
                f"application {app_id!r} is not registered for this profile",
                details={"allowed_apps": sorted(registry)},
            )
        return registry[app_id]

    # -- assets ---------------------------------------------------------------------
    def require_base_image(self) -> Path:
        """Validate the configured base image without letting the agent influence it."""
        base = Path(self.profile.images.base)
        if not base.is_absolute():
            raise PaletteError(
                ErrorCode.IMAGE_INVALID, "the profile's base image path must be absolute"
            )
        if not base.is_file():
            raise PaletteError(
                ErrorCode.IMAGE_MISSING,
                "the profile's base image is not present on this host",
                details={"profile": self.profile.profile_id},
            )
        if _is_reparse_point(base):
            raise PaletteError(ErrorCode.PATH_DENIED, "the base image must not be a link")
        suffix = base.suffix.lower().lstrip(".")
        if suffix != self.profile.images.format:
            raise PaletteError(
                ErrorCode.IMAGE_INVALID,
                f"base image extension {suffix!r} does not match the declared format "
                f"{self.profile.images.format!r}",
            )
        return base

    def require_network_offline(self) -> None:
        if self.profile.network.mode != "offline":
            raise PaletteError(
                ErrorCode.POLICY_DENIED,
                "this build only allows the offline network mode",
                details={"mode": self.profile.network.mode},
            )


def sanitise_for_log(values: Iterable[str], *, keep: int = 8) -> list[str]:
    """Reduce a list of identifiers to something safe for a log line."""
    return [validate_identifier(value) for value in list(values)[:keep] if isinstance(value, str)]
