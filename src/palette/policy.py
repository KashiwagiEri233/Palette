"""Authorization, path confinement and capability gating.

Two rules are enforced here, and nowhere else:

1. **Managed files stay inside the managed runtime root.** Every Palette-owned location is
   composed from trusted configuration plus a service-generated identifier, then checked
   before it is read, written or deleted.
2. **Power comes from trusted configuration, not from the caller's own claims.** A caller
   cannot grant itself the reset permission by sending ``confirmed=true``.

Caller-supplied *host* material — a base image path, a CD-ROM, raw QEMU arguments — is a
separate axis with its own switches in ``lifecycle``. Rule 1 still holds for everything
Palette creates; what changes when those switches are on is that the caller, rather than the
profile, decides what QEMU is pointed at. That is a deliberate removal of a boundary, not a
gap, and ``docs/threat-model.md`` records it as one.
"""

from __future__ import annotations

import ntpath
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
    "host.image": "caller-supplied base image",
    "host.iso": "caller-supplied CD-ROM or extra disk",
    "host.qemu_args": "caller-supplied QEMU arguments",
    "guest.input_text": "vm_guest_input_text",
    "guest.launch_app": "vm_guest_launch_app",
    "guest.filesystem": "vm_guest_filesystem",
    "guest.powershell": "vm_guest_powershell",
    "guest.process": "vm_guest_process",
    "guest.registry": "vm_guest_registry",
    "guest.app": "vm_guest_app",
    "guest.snapshot": "vm_guest_snapshot",
    "guest.batch": "vm_guest_multi_select / vm_guest_multi_edit",
    "guest.clipboard": "vm_guest_clipboard_get / vm_guest_clipboard_set",
    "guest.file": "vm_guest_file_open / read / write / close",
    "guest.resize": "vm_guest_resize",
    "guest.sync_time": "vm_guest_sync_time",
}

#: The kinds of host-side material a caller can hand the service. Each has its own switch
#: and its own consequence, so they are granted and refused individually.
MaterialKind = Literal["image", "iso", "qemu_args"]

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
            if os.path.isabs(text) or ntpath.splitdrive(text)[0] or text.startswith(("/", "\\")):
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
        standard = {
            "guest.clipboard": self.profile.guest_desktop.clipboard,
            "guest.file": self.profile.guest_agent.enabled,
            "guest.resize": self.profile.guest_desktop.resize,
            "guest.sync_time": self.profile.guest_agent.enabled,
        }
        if name in standard:
            if not standard[name]:
                raise PaletteError(
                    ErrorCode.GUEST_TOOL_UNAVAILABLE, "guest capability is disabled in this profile"
                )
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
        """The capability list reported by ``vm_status``.

        Every entry here is a grant the profile actually makes, including the host-material
        ones. Reporting them is the point: a client can see that this profile lets a caller
        name a base image or append QEMU arguments, rather than discovering it by trying.
        """
        available = ["observe", "control"]
        for operation, flag in (
            ("lifecycle.start", self.profile.lifecycle.allow_agent_start),
            ("lifecycle.stop", self.profile.lifecycle.allow_agent_stop),
            ("lifecycle.reset", self.profile.lifecycle.allow_agent_reset),
            ("host.image", self.profile.lifecycle.allow_agent_images),
            ("host.iso", self.profile.lifecycle.allow_agent_iso),
            ("host.qemu_args", self.profile.lifecycle.allow_agent_qemu_args),
        ):
            if flag:
                available.append(operation)
        if self.profile.guest_tools.enabled:
            available.extend(
                capability
                for capability in sorted(self.profile.guest_tools.capabilities)
                if capability != "guest.clipboard" or self.profile.guest_desktop.clipboard
            )
        for capability, enabled in (
            ("guest.input_text", self.profile.guest_desktop.clipboard),
            ("guest.clipboard", self.profile.guest_desktop.clipboard),
            ("guest.resize", self.profile.guest_desktop.resize),
            ("guest.file", self.profile.guest_agent.enabled),
            ("guest.sync_time", self.profile.guest_agent.enabled),
        ):
            if enabled and capability not in available:
                available.append(capability)
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

    def require_network_supported(self) -> None:
        """Refuse a network mode this build cannot actually implement.

        ``internet-allowlist`` is the only mode that would express an egress *policy*, and
        enforcing it needs a gateway, proxy or firewall that does not exist here. Saying so
        is the point: user-mode NAT lets a guest out and is not an allowlist.
        """
        mode = self.profile.network.mode
        if mode == "internet-allowlist":
            raise PaletteError(
                ErrorCode.NOT_IMPLEMENTED,
                "the internet-allowlist network mode needs a controlled egress gateway, which "
                "this build does not provide; user-mode NAT is not an allowlist",
                details={"mode": mode, "supported": ["offline", "restricted-lab", "nat"]},
            )

    def require_host_material(self, kind: MaterialKind, *, actor: str) -> None:
        """Gate caller-supplied host material: an image path, a CD-ROM, or raw QEMU args.

        Operators may always supply these — they are the ones who wrote the profile. A
        caller acting as an agent needs the matching switch, because each kind hands over
        something the profile would otherwise have pinned.
        """
        switch = {
            "image": self.profile.lifecycle.allow_agent_images,
            "iso": self.profile.lifecycle.allow_agent_iso,
            "qemu_args": self.profile.lifecycle.allow_agent_qemu_args,
        }[kind]
        if actor == "operator" or switch:
            return
        raise PaletteError(
            ErrorCode.POLICY_DENIED,
            f"this profile does not allow callers to supply {kind}",
            details={"kind": kind, "switch": f"allow_agent_{kind}"},
        )


def sanitise_for_log(values: Iterable[str], *, keep: int = 8) -> list[str]:
    """Reduce a list of identifiers to something safe for a log line."""
    return [validate_identifier(value) for value in list(values)[:keep] if isinstance(value, str)]
