"""Trusted configuration: host settings and per-profile VM definitions.

Configuration is trusted input; agent input never reaches this module. Profiles are
TOML files under ``profiles/`` and unknown keys are rejected, so a mistyped control
(for example ``allow_agent_reset`` spelled wrongly) fails loudly instead of leaving a
default in place.

Host material and enhancement defaults are configurable. Accelerator fallback remains
disabled; software emulation is selected explicitly through a TCG profile.
"""

from __future__ import annotations

import ntpath
import os
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .errors import ErrorCode, PaletteError
from .platforms import native_profile_id


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class FirmwareConfig(_Strict):
    """Firmware code and variable store. The variable store is copied per instance."""

    code: str | None = None
    vars: str | None = None


class QemuConfig(_Strict):
    arch: Literal["x86_64", "aarch64"] = "x86_64"
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
    accelerator: Literal["whpx", "kvm", "hvf", "tcg"] = "whpx"
    allow_accelerator_fallback: bool = Field(
        default=False,
        description="Must stay false: a silent fallback to TCG changes the isolation story",
    )
    machine: str = "q35"
    cpu: str | None = None
    firmware: FirmwareConfig = Field(default_factory=FirmwareConfig)
    extra_args: list[str] = Field(
        default_factory=list,
        description=(
            "Static QEMU arguments from the operator. Not filtered: an operator writing a "
            "profile is trusted, and once callers may pass their own arguments (see "
            "LifecyclePolicy.allow_agent_qemu_args) an argument filter here would be "
            "decorative. Adding a monitor, VNC or SPICE channel breaks the single-writer "
            "assumption, so build_launch_plan warns when it sees one."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _target_defaults(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        arch = data.get("arch", "x86_64")
        data.setdefault("binary", f"qemu-system-{arch}")
        data.setdefault("machine", "virt" if arch == "aarch64" else "q35")
        if data.get("accelerator") == "tcg":
            data.setdefault("cpu", "max")
        elif arch == "aarch64" or data.get("accelerator") == "hvf":
            data.setdefault("cpu", "host")
        return data

    @model_validator(mode="after")
    def _no_silent_fallback(self) -> QemuConfig:
        if self.allow_accelerator_fallback:
            raise ValueError(
                "allow_accelerator_fallback must be false: missing acceleration is a diagnosed "
                "failure, not a silent degradation"
            )
        if self.accelerator == "tcg" and self.cpu == "host":
            raise ValueError("TCG cannot use cpu=host; use max or an emulated CPU model")
        if self.arch == "aarch64" and self.machine.split(",", 1)[0] != "virt":
            raise ValueError("aarch64 profiles require the virt machine")
        if self.arch == "x86_64" and self.machine.split(",", 1)[0] == "virt":
            raise ValueError("the virt machine requires an aarch64 guest")
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
    default_resolution: bool = False
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
    process_max_runtime_seconds: float | None = Field(default=None, gt=0)
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
    #: How long a previous capture may stand as evidence of the current framebuffer size.
    #: Zero — the default — means every coordinate action looks again before it moves the
    #: pointer, which is what the design asks for: a click built on a frame from a surface
    #: that has since changed lands somewhere else. A non-zero value trades that for one
    #: fewer screendump per action, and opens a window in which a resize goes unnoticed.
    size_check_interval_seconds: float = Field(default=0.0, ge=0)
    keep_frames: bool = Field(
        default=False, description="Persist captured frames on disk; off by default"
    )


class NetworkConfig(_Strict):
    """How the guest reaches a network.

    ``offline`` attaches no NIC at all. ``restricted-lab`` and ``nat`` both attach one
    through QEMU user-mode networking: the first adds ``restrict=on``, which stops the
    guest routing past the slirp sandbox, the second does not and therefore gives the guest
    the same outbound reach the host has.

    ``internet-allowlist`` is declared because it is the only mode that would actually
    express an egress *policy*, and it is refused at start: this build has no gateway,
    proxy or firewall to enforce an allowlist, and user-mode NAT is not one.
    """

    mode: Literal["offline", "restricted-lab", "nat", "internet-allowlist"] = "restricted-lab"
    #: Pins the guest MAC, for a lab network where the address matters.
    mac: str | None = None
    model: Literal["virtio-net-pci", "e1000", "rtl8139"] | None = None
    #: Additional QEMU ``-nic`` settings, passed through verbatim for the chosen mode.
    extra_options: str | None = None
    #: Port forwards for `restricted-lab`/`nat`, e.g. ["tcp::8080-:80"]. Recorded in the
    #: audit trail at start, because an explicit forward punches through the user-mode
    #: sandbox that the other settings otherwise provide.
    hostfwd: list[str] = Field(default_factory=list)

    @property
    def attaches_nic(self) -> bool:
        return self.mode != "offline"

    def qemu_nic_argument(self) -> str:
        """The ``-nic`` value for this mode."""
        if self.mode == "offline":
            return "none"
        parts = ["user"]
        if self.model:
            parts.append(f"model={self.model}")
        if self.mode == "restricted-lab":
            parts.append("restrict=on")
        if self.mac:
            parts.append(f"mac={self.mac}")
        parts.extend(f"hostfwd={forward}" for forward in self.hostfwd)
        if self.extra_options:
            parts.append(self.extra_options)
        return ",".join(parts)


class AudioConfig(_Strict):
    enabled: bool = False
    backend: Literal["coreaudio", "dsound", "pipewire", "pa", "alsa"] | None = None


class QemuGuestAgentConfig(_Strict):
    enabled: bool = False
    request_timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    max_file_bytes: int = Field(default=16 * 1024 * 1024, ge=1, le=64 * 1024 * 1024)


class GuestDesktopConfig(_Strict):
    clipboard: bool = False
    resize: bool = False
    shared_folders: bool = False
    max_clipboard_bytes: int = Field(default=48 * 1024, ge=1, le=48 * 1024)


class SharedFolderConfig(_Strict):
    tag: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,30}$")
    path: str = Field(min_length=1, max_length=4096)
    read_only: bool = True


class TextConfig(_Strict):
    mode: Literal["keyboard"] = "keyboard"
    max_chars: int = Field(default=256, ge=1, le=4096)
    char_interval_ms: int = Field(default=12, ge=0, le=1000)
    #: Typing long strings is rate limited rather than sharing the click latency budget.
    max_total_ms: int = Field(default=15000, ge=100, le=120000)


class TimeoutConfig(_Strict):
    qmp_command_seconds: float = Field(default=10.0, gt=0)
    qmp_handshake_seconds: float = Field(default=10.0, gt=0)
    startup_seconds: float = Field(default=120.0, gt=0)
    boot_poll_seconds: float = Field(default=1.0, gt=0)
    shutdown_seconds: float = Field(default=60.0, gt=0)
    force_kill_seconds: float = Field(default=10.0, gt=0)
    settle_after_input_ms: int = Field(default=120, ge=0, le=5000)
    double_click_gap_ms: int = Field(default=90, ge=20, le=500)
    click_hold_ms: int = Field(default=40, ge=0, le=1000)


class LifecyclePolicy(_Strict):
    """Who may drive the VM lifecycle, and who may hand the service host-side material.

    The ``allow_agent_*`` flags below default to what an operator who wants full control
    asks for. Every one of them is a switch: turn them off in a profile to get the stricter
    posture back, and note that ``allow_agent_images`` and ``allow_agent_qemu_args`` are the
    two that matter most — together they remove the host boundary entirely.
    """

    allow_agent_start: bool = True
    allow_agent_stop: bool = False
    allow_agent_reset: bool = False
    allow_agent_force_stop: bool = False
    operator_only_tools: bool = True

    #: Let a request name the base image, bypassing the profile's pinned path and digest.
    #: The named file becomes the read-only backing store, so it must be one the caller is
    #: willing to expose; the service still copies nothing into it.
    allow_agent_images: bool = True
    #: Let a request append raw QEMU arguments. This is host code execution by another
    #: name: `-chardev`, `-device`, `-blockdev` and friends can read and write host files,
    #: and `-mon`/`-vnc` add a second, unauthenticated way to drive the guest.
    allow_agent_qemu_args: bool = True
    #: Let a request attach a CD-ROM or extra disk from an arbitrary host path.
    allow_agent_iso: bool = True


class GuestToolsConfig(_Strict):
    """The in-guest enhancement channel.

    Enabled by default, with both registered capabilities granted, because the point of the
    channel is to be usable. It still delivers nothing until a guest-side service answers
    the handshake: `vm_status` reports the two online states separately, and a call against
    a guest with no service fails as unavailable rather than falling back to keystrokes.
    """

    enabled: bool = True
    protocol_version: str = "1.0"
    capabilities: list[str] = Field(
        default_factory=lambda: ["guest.input_text", "guest.launch_app"],
        description="Registered capabilities to request; the guest must also offer them",
    )
    input_methods: list[str] = Field(
        default_factory=lambda: ["native_text"],
        description="Preferred text backends, in order of preference",
    )
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
    audio: AudioConfig = Field(default_factory=AudioConfig)
    guest_agent: QemuGuestAgentConfig = Field(default_factory=QemuGuestAgentConfig)
    guest_desktop: GuestDesktopConfig = Field(default_factory=GuestDesktopConfig)
    shared_folders: list[SharedFolderConfig] = Field(default_factory=list, max_length=16)
    text: TextConfig = Field(default_factory=TextConfig)
    timeouts: TimeoutConfig = Field(default_factory=TimeoutConfig)
    lifecycle: LifecyclePolicy = Field(default_factory=LifecyclePolicy)
    guest_tools: GuestToolsConfig = Field(default_factory=GuestToolsConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    frame_ttl_seconds: float = Field(default=30.0, gt=0, le=600)

    @model_validator(mode="after")
    def _shared_tags(self) -> Profile:
        tags = [folder.tag for folder in self.shared_folders]
        if len(tags) != len(set(tags)):
            raise ValueError("shared folder tags must be unique")
        return self

    @model_validator(mode="before")
    @classmethod
    def _target_devices(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        qemu = value.get("qemu", {})
        arch = (
            qemu.arch
            if isinstance(qemu, QemuConfig)
            else (qemu.get("arch", "x86_64") if isinstance(qemu, dict) else None)
        )
        if arch == "aarch64" and isinstance(value.get("devices", {}), dict):
            data = dict(value)
            devices = dict(data.get("devices", {}))
            devices.setdefault("display_model", "virtio-gpu-pci")
            data["devices"] = devices
            return data
        return value

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
        if wanted == "auto":
            wanted = native_profile_id()
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
            image = profile.images.model_copy(
                update={"base": _trusted_path(profile.images.base, config_root)}
            )
            firmware = profile.qemu.firmware.model_copy(
                update={
                    key: _trusted_path(value, config_root)
                    for key, value in profile.qemu.firmware.model_dump().items()
                    if value is not None
                }
            )
            profile = profile.model_copy(
                update={
                    "images": image,
                    "qemu": profile.qemu.model_copy(update={"firmware": firmware}),
                    "shared_folders": [
                        folder.model_copy(update={"path": _trusted_path(folder.path, config_root)})
                        for folder in profile.shared_folders
                    ],
                }
            )
            profiles[profile.profile_id] = profile

    try:
        host = HostConfig.model_validate(host_data.get("host", {}))
    except Exception as exc:
        raise PaletteError(ErrorCode.INVALID_ARGUMENT, f"palette.toml is invalid: {exc}") from exc

    config = PaletteConfig(host=host, profiles=profiles, config_root=config_root)
    selected_default = (
        native_profile_id()
        if config.host.default_profile == "auto"
        else config.host.default_profile
    )
    if config.profiles and selected_default not in config.profiles:
        raise PaletteError(
            ErrorCode.INVALID_ARGUMENT,
            f"default_profile {config.host.default_profile!r} is not defined",
            details={"known_profiles": config.profile_ids()},
        )
    return config


def _trusted_path(value: str, root: Path) -> str:
    # Preserve foreign absolute paths in profiles for other hosts; only the selected
    # profile is used at runtime. Relative paths never depend on the launch directory.
    if Path(value).is_absolute() or ntpath.isabs(value):
        return value
    return str((root / value).resolve())


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise PaletteError(
            ErrorCode.INVALID_ARGUMENT, f"{path.name} is not valid TOML: {exc}"
        ) from exc
