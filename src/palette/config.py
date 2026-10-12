"""Trusted configuration: host settings and per-profile VM definitions.

Configuration is trusted input; agent input never reaches this module. Profiles are
TOML files under ``profiles/`` and unknown keys are rejected, so a mistyped control
(for example ``allow_agent_reset`` spelled wrongly) fails loudly instead of leaving a
default in place.

Every safety-relevant default here matches the plan: offline, no TCG fallback, no
guest enhancement channel, no agent lifecycle authority.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .errors import ErrorCode, PaletteError


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FirmwareConfig(_Strict):
    """Firmware code and variable store. The variable store is copied per instance."""

    code: str | None = None
    vars: str | None = None


class QemuConfig(_Strict):
    binary: str = "qemu-system-x86_64"
    argv_prefix: list[str] = Field(
        default_factory=list,
        description="Arguments placed before Palette's own; used for wrapper/test binaries",
    )
    qemu_img: str | None = Field(
        default=None,
        description=(
            "Path to qemu-img. Defaults to a sibling of the emulator, then PATH. It is "
            "named separately because overlay creation must never fall back to the "
            "emulator binary if the helper is missing."
        ),
    )
    qemu_img_prefix: list[str] = Field(
        default_factory=list,
        description="Arguments placed before qemu-img's own; mirrors argv_prefix",
    )
    version_pin: str | None = None
    accelerator: Literal["whpx", "kvm", "tcg"] = "whpx"
    allow_accelerator_fallback: bool = Field(
        default=False,
        description="Must stay false: a silent fallback to TCG changes the isolation story",
    )
    machine: str = "q35"
    cpu: str | None = None
    firmware: FirmwareConfig = Field(default_factory=FirmwareConfig)
    extra_args: list[str] = Field(
        default_factory=list,
        description="Reviewed static arguments only; never agent-supplied",
    )

    @model_validator(mode="after")
    def _no_silent_fallback(self) -> QemuConfig:
        if self.allow_accelerator_fallback:
            raise ValueError(
                "allow_accelerator_fallback must be false: missing acceleration is a diagnosed "
                "failure, not a silent degradation"
            )
        return self

    @model_validator(mode="after")
    def _no_injection(self) -> QemuConfig:
        for arg in self.extra_args:
            lowered = arg.lower()
            if any(token in lowered for token in ("chardev", "-mon", "monitor", "telnet", "vnc")):
                raise ValueError(
                    f"profile must not add monitor or display channels to QEMU args: {arg!r}"
                )
        return self


class ImagesConfig(_Strict):
    base: str = Field(description="Absolute path to the read-only base image")
    format: Literal["qcow2", "raw"] = "qcow2"
    sha256: str | None = None
    overlay_dir: str | None = Field(
        default=None,
        description="Where instance overlays live; defaults to <runtime>/vms/<vm_id>/",
    )
    size_gib: int = Field(default=24, ge=1, le=4096)


class DeviceConfig(_Strict):
    display_id: str = "video0"
    display_model: str = "VGA"
    keyboard_id: str = "agent-kbd"
    pointer_id: str = "agent-pointer"
    head: int = Field(default=0, ge=0)


class ResourcesConfig(_Strict):
    vcpus: int = Field(default=2, ge=1, le=64)
    memory_mib: int = Field(default=4096, ge=256)
    max_queue_depth: int = Field(default=32, ge=1, le=1024)
    max_queued_wait_seconds: float = Field(default=5.0, gt=0)
    max_action_seconds: float = Field(default=30.0, gt=0)
    #: Hard ceiling on the QEMU process itself; the guest RAM figure is not a host cap.
    process_memory_limit_mib: int | None = Field(default=8192, ge=256)
    process_max_runtime_seconds: float | None = None
    max_overlay_bytes: int | None = None


class DisplayConfig(_Strict):
    width: int = Field(default=1280, ge=2)
    height: int = Field(default=800, ge=2)
    scale_percent: Literal[100, 125, 150] = 100
    keymap: str = "us"


class CaptureConfig(_Strict):
    prefer_format: Literal["png", "ppm"] = "png"
    allow_ppm_fallback: bool = True
    max_long_edge: int = Field(default=1280, ge=2)
    max_bytes: int = Field(default=4 * 1024 * 1024, ge=1024)
    timeout_seconds: float = Field(default=10.0, gt=0)
    #: How long a previous capture may be reused as evidence of the current framebuffer
    #: size before a coordinate action forces a fresh size check.
    size_check_interval_seconds: float = Field(default=5.0, ge=0)
    keep_frames: bool = Field(
        default=False, description="Persist captured frames on disk; off by default"
    )


class NetworkConfig(_Strict):
    mode: Literal["offline", "restricted-lab"] = "offline"
    restrict: bool = True

    @model_validator(mode="after")
    def _offline_has_no_nic(self) -> NetworkConfig:
        if self.mode != "offline" and self.restrict is False:
            raise ValueError("a networked profile must keep user-networking restrictions enabled")
        return self


class TextConfig(_Strict):
    mode: Literal["keyboard"] = "keyboard"
    max_chars: int = Field(default=256, ge=1, le=4096)
    char_interval_ms: int = Field(default=12, ge=0, le=1000)
    #: Typing long strings is rate limited rather than sharing the click latency budget.
    max_total_ms: int = Field(default=15000, ge=100, le=120000)


class TimeoutConfig(_Strict):
    qmp_command_seconds: float = 10.0
    qmp_handshake_seconds: float = 10.0
    startup_seconds: float = 120.0
    boot_poll_seconds: float = 1.0
    shutdown_seconds: float = 60.0
    force_kill_seconds: float = 10.0
    settle_after_input_ms: int = Field(default=120, ge=0, le=5000)
    double_click_gap_ms: int = Field(default=90, ge=20, le=500)
    click_hold_ms: int = Field(default=40, ge=0, le=1000)


class LifecyclePolicy(_Strict):
    """Who may drive the VM lifecycle. Agents get observation and control only by default."""

    allow_agent_start: bool = False
    allow_agent_stop: bool = False
    allow_agent_reset: bool = False
    allow_agent_force_stop: bool = False
    operator_only_tools: bool = True


class GuestToolsConfig(_Strict):
    """The retained enhancement channel: defined here, disabled by default."""

    enabled: bool = False
    protocol_version: str = "1.0"
    capabilities: list[str] = Field(default_factory=list)
    input_methods: list[str] = Field(default_factory=list)
    allowed_apps: dict[str, dict[str, Any]] = Field(default_factory=dict)
    max_text_chars: int = Field(default=4096, ge=1)
    request_timeout_seconds: float = Field(default=20.0, gt=0)
    max_message_bytes: int = Field(default=1 << 20, ge=1024)
    target_session: str = "interactive"

    @model_validator(mode="after")
    def _consistency(self) -> GuestToolsConfig:
        if self.enabled and not self.capabilities:
            raise ValueError("seeding guest_tools without a capability list is meaningless")
        unknown = set(self.capabilities) - {"guest.input_text", "guest.launch_app"}
        if unknown:
            raise ValueError(f"unregistered guest capability: {sorted(unknown)}")
        return self


class RetentionConfig(_Strict):
    keep_overlays: bool = True
    max_overlays: int = Field(
        default=8,
        ge=1,
        description=(
            "How many stopped instance layers to keep besides the running one, so results "
            "can be retrieved without letting the disk grow without bound"
        ),
    )
    persist_screenshots: bool = False
    audit_max_bytes: int = Field(default=8 * 1024 * 1024, ge=1024)
    audit_max_files: int = Field(default=5, ge=1)


class Profile(_Strict):
    """A complete VM definition. Nothing here is derived from agent input."""

    profile_id: str
    description: str = ""
    qemu: QemuConfig
    images: ImagesConfig
    devices: DeviceConfig = Field(default_factory=DeviceConfig)
    resources: ResourcesConfig = Field(default_factory=ResourcesConfig)
    display: DisplayConfig = Field(default_factory=DisplayConfig)
    capture: CaptureConfig = Field(default_factory=CaptureConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    text: TextConfig = Field(default_factory=TextConfig)
    timeouts: TimeoutConfig = Field(default_factory=TimeoutConfig)
    lifecycle: LifecyclePolicy = Field(default_factory=LifecyclePolicy)
    guest_tools: GuestToolsConfig = Field(default_factory=GuestToolsConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    frame_ttl_seconds: float = Field(default=30.0, gt=0, le=600)

    @property
    def mode(self) -> Literal["visual_only", "hybrid"]:
        return "hybrid" if self.guest_tools.enabled else "visual_only"

    def base_image_path(self) -> Path:
        return Path(self.images.base)


class HostConfig(_Strict):
    """Host-level settings that are not per-VM."""

    default_profile: str = "default"
    runtime_root: str = ".runtime"
    qemu_search_paths: list[str] = Field(
        default_factory=lambda: [
            r"C:\Program Files\qemu",
            r"C:\Program Files\qemu\bin",
        ]
    )
    audit_enabled: bool = True
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    #: Reserved for the operator identity that authorises destructive lifecycle actions
    #: when no interactive confirmation channel exists.
    operator_authorized: bool = True


class PaletteConfig(_Strict):
    """The whole trusted configuration: host settings plus every known profile."""

    host: HostConfig = Field(default_factory=HostConfig)
    profiles: dict[str, Profile] = Field(default_factory=dict)
    config_root: Path = Field(default_factory=lambda: Path.cwd())

    @property
    def runtime_root(self) -> Path:
        root = Path(self.host.runtime_root)
        if not root.is_absolute():
            root = self.config_root / root
        return root.resolve()

    def profile(self, profile_id: str | None) -> Profile:
        wanted = profile_id or self.host.default_profile
        try:
            return self.profiles[wanted]
        except KeyError:
            raise PaletteError(
                ErrorCode.INVALID_ARGUMENT,
                f"unknown profile {wanted!r}",
                details={"known_profiles": sorted(self.profiles)},
            ) from None

    def profile_ids(self) -> list[str]:
        return sorted(self.profiles)


DEFAULT_PROFILE_NAME = "default"


def find_config_root(start: Path | None = None) -> Path:
    """Locate the repository root that holds ``profiles/``.

    Explicit environment override wins, then the nearest ancestor containing
    ``profiles/`` or ``pyproject.toml``.
    """
    override = os.environ.get("PALETTE_ROOT")
    if override:
        return Path(override).resolve()
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / "profiles").is_dir() or (candidate / "pyproject.toml").is_file():
            return candidate
    return current


def load_config(root: Path | None = None, *, profiles_dir: Path | None = None) -> PaletteConfig:
    """Load ``palette.toml`` plus every profile file under ``profiles/``."""
    config_root = (root or find_config_root()).resolve()
    profiles_path = profiles_dir or (config_root / "profiles")

    host_data: dict[str, Any] = {}
    host_file = config_root / "palette.toml"
    if host_file.is_file():
        host_data = _read_toml(host_file)

    profiles: dict[str, Profile] = {}
    if profiles_path.is_dir():
        for path in sorted(profiles_path.glob("*.toml")):
            data = _read_toml(path)
            if data.get("template"):
                # Profiles may template from each other; the marker key is not a field.
                data.pop("template", None)
            data["profile_id"] = data.get("profile_id") or path.stem
            try:
                profile = Profile.model_validate(data)
            except Exception as exc:
                raise PaletteError(
                    ErrorCode.INVALID_ARGUMENT,
                    f"profile file {path.name} is invalid: {exc}",
                ) from exc
            if profile.profile_id != path.stem:
                raise PaletteError(
                    ErrorCode.INVALID_ARGUMENT,
                    f"profile file {path.name} declares profile_id {profile.profile_id!r}",
                )
            profiles[profile.profile_id] = profile

    try:
        host = HostConfig.model_validate(host_data.get("host", {}))
    except Exception as exc:
        raise PaletteError(ErrorCode.INVALID_ARGUMENT, f"palette.toml is invalid: {exc}") from exc

    config = PaletteConfig(host=host, profiles=profiles, config_root=config_root)
    if config.profiles and config.host.default_profile not in config.profiles:
        raise PaletteError(
            ErrorCode.INVALID_ARGUMENT,
            f"default_profile {config.host.default_profile!r} is not defined",
            details={"known_profiles": config.profile_ids()},
        )
    return config


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise PaletteError(
            ErrorCode.INVALID_ARGUMENT, f"{path.name} is not valid TOML: {exc}"
        ) from exc
