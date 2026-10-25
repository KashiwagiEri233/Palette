"""Locating QEMU, building its argument vector, and starting it safely.

Three rules are enforced here:

* **The argument vector is a list, never a string.** No shell is involved, so nothing an
  agent can influence is ever subject to quoting or metacharacter rules.
* **The environment is a whitelist.** QEMU inherits the handful of variables it needs to
  find its own DLLs and temporary directory, not the operator's tokens and credentials.
* **Missing acceleration is diagnosed, not degraded.** If WHPX is requested and
  unavailable, the start fails; falling back to TCG would silently change the isolation
  story that the plan's security section depends on.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import locale
import logging
import os
import re
import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ..config import HostConfig, Profile
from ..errors import ErrorCode, PaletteError
from ..platforms import validate_target
from .managed import ManagedProcess, SupervisedProcess, finish_cleanup, terminate_process_group

logger = logging.getLogger("palette.vm.launcher")

#: QEMU's stdout carries QMP; the reader needs room for ``query-qmp-schema``.
STREAM_LIMIT = 16 * 1024 * 1024


def spawn_daemon(config_root: Path) -> None:
    """Start the Python runtime independently of the launching UI or stdio client."""
    from ..config import load_config

    config = load_config(config_root)
    config.runtime_root.mkdir(parents=True, exist_ok=True)
    log_path = config.runtime_root / "daemon.log"
    fd = os.open(
        log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    with os.fdopen(fd, "ab") as log:
        subprocess.Popen(  # noqa: S603 - fixed module and argument vector, never a shell
            [sys.executable, "-m", "palette.cli", "--root", str(config_root), "daemon", "run"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=log,
            cwd=config_root,
            env=build_child_env(audio=True),
            creationflags=CREATION_FLAGS,
            start_new_session=START_NEW_SESSION,
        )


#: Only these variables are handed to the child. Everything else — including any
#: credential the operator happens to have exported — stays out of the VM's process.
ENV_WHITELIST = (
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "TEMP",
    "TMP",
    "TMPDIR",
    "PATH",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "PROCESSOR_IDENTIFIER",
    "LANG",
    "LC_ALL",
)

_VERSION_RE = re.compile(r"QEMU emulator version (\S+)")
_DEVICE_RE = re.compile(r'^\s*name "([^"]+)"', re.MULTILINE)


@dataclass(slots=True)
class QemuProbe:
    """What ``doctor`` and the launcher learned about one QEMU build."""

    binary: str
    found: bool = False
    version: str | None = None
    accelerators: list[str] = field(default_factory=list)
    devices: list[str] = field(default_factory=list)
    error: str | None = None

    def supports_device(self, name: str) -> bool:
        if not self.devices:
            return True  # unknown rather than absent
        return any(entry == name or entry.startswith(f"{name} ") for entry in self.devices)

    def as_dict(self) -> dict[str, object]:
        return {
            "binary": self.binary,
            "found": self.found,
            "version": self.version,
            "accelerators": self.accelerators,
            "devices_known": bool(self.devices),
            "error": self.error,
        }


def build_child_env(*, audio: bool = False) -> dict[str, str]:
    env = {key: os.environ[key] for key in ENV_WHITELIST if key in os.environ}
    if audio and sys.platform == "linux":
        for key in ("XDG_RUNTIME_DIR", "PULSE_SERVER", "PIPEWIRE_REMOTE"):
            if key in os.environ:
                env[key] = os.environ[key]
    return env


#: CREATE_NO_WINDOW keeps QEMU hidden on Windows: the agent operates a VM, never the host
#: desktop, and a stray console window would also steal host focus. On POSIX the process
#: gets its own session instead. Both parameters exist on both platforms and the
#: irrelevant one is ignored, so they can be passed unconditionally and type-check.
CREATION_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
START_NEW_SESSION = sys.platform != "win32"
IS_WINDOWS = sys.platform == "win32"


def run_sync(
    argv: Sequence[str],
    *,
    timeout: float,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a helper to completion, capturing output.

    A single call site for the flags means the "hidden, no window, whitelisted
    environment" policy cannot drift between the four places that start a process.
    """
    return subprocess.run(  # noqa: S603 - fixed argv, never a shell, never agent input
        list(argv),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=build_child_env(),
        cwd=str(cwd) if cwd else None,
        check=False,
        creationflags=CREATION_FLAGS,
        start_new_session=START_NEW_SESSION,
    )


def locate_binary(profile: Profile, host: HostConfig) -> str | None:
    """Resolve the configured QEMU binary without executing anything."""
    configured = profile.qemu.binary
    candidate = Path(configured)
    name = candidate.name.removesuffix(".exe")
    if name.startswith("qemu-system-") and name != f"qemu-system-{profile.qemu.arch}":
        raise PaletteError(
            ErrorCode.INVALID_ARGUMENT,
            f"qemu.binary {candidate.name!r} does not match guest architecture {profile.qemu.arch}",
        )
    if candidate.is_file():
        return str(candidate)
    found = shutil.which(configured)
    if found:
        return found
    # An explicit executable is authoritative. Never replace a missing binary with
    # another architecture, or silently use a different installation than requested.
    if candidate.is_absolute() or candidate.parent != Path("."):
        return None
    names = (configured, f"{configured}.exe") if not configured.endswith(".exe") else (configured,)
    for search in host.qemu_search_paths:
        directory = Path(search)
        if not directory.is_dir():
            continue
        for name in names:
            candidate = directory / name
            if candidate.is_file():
                return str(candidate)
        # A qemu release directory nests its executables one level down.
        for nested in sorted(directory.glob("qemu-*")):
            if nested.is_dir():
                for name in names:
                    candidate = nested / name
                    if candidate.is_file():
                        return str(candidate)
    return None


def probe_binary(
    binary: str,
    *,
    timeout: float = 20.0,
    with_devices: bool = False,
    prefix: Sequence[str] = (),
) -> QemuProbe:
    """Run ``--version`` (and optionally ``-device help``) to characterise the build.

    ``prefix`` carries the profile's ``argv_prefix``, so a wrapper or launcher script is
    probed the same way it will later be started. Probing the bare executable would
    interrogate the wrapper's interpreter instead of QEMU.
    """
    probe = QemuProbe(binary=binary)
    launch = [binary, *prefix]
    try:
        result = run_sync([*launch, "--version"], timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        probe.error = f"{type(exc).__name__}: {exc}"
        return probe
    if result.returncode != 0:
        probe.error = f"--version exited with {result.returncode}"
        return probe
    output = result.stdout or ""
    match = _VERSION_RE.search(output)
    probe.found = True
    probe.version = match.group(1) if match else output.splitlines()[0].strip() if output else None

    try:
        accel = run_sync([*launch, "-accel", "help"], timeout=timeout)
        probe.accelerators = sorted(
            {
                line.strip()
                for line in (accel.stdout or "").splitlines()
                if line.strip() and not line.strip().startswith("Accelerators")
            }
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("accelerator probe failed: %s", exc)

    if with_devices:
        try:
            devices = run_sync([*launch, "-device", "help"], timeout=timeout * 2)
            probe.devices = _DEVICE_RE.findall(devices.stdout or "")
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("device probe failed: %s", exc)
    return probe


async def probe_binary_async(
    binary: str, *, timeout: float = 20.0, prefix: Sequence[str] = ()
) -> QemuProbe:
    """Probe without blocking other VMs; run_tool owns cancellation and reaping."""
    probe = QemuProbe(binary=binary)
    launch = [binary, *prefix]
    try:
        code, output, _ = await run_tool([*launch, "--version"], timeout=timeout)
        if code:
            probe.error = f"--version exited with {code}"
            return probe
        match = _VERSION_RE.search(output)
        probe.found = True
        probe.version = (
            match.group(1) if match else output.splitlines()[0].strip() if output else None
        )
        code, output, _ = await run_tool([*launch, "-accel", "help"], timeout=timeout)
        if not code:
            probe.accelerators = sorted(
                {
                    line.strip()
                    for line in output.splitlines()
                    if line.strip() and not line.strip().startswith("Accelerators")
                }
            )
    except (OSError, TimeoutError, PaletteError) as exc:
        if not probe.found:
            probe.error = type(exc).__name__
    return probe


def require_accelerator(profile: Profile, probe: QemuProbe) -> str:
    """Return the accelerator to use, or fail loudly.

    An explicitly trusted TCG profile is the only way to run without hardware
    acceleration, and it is opt-in through the profile rather than a runtime fallback.
    """
    requested = profile.qemu.accelerator
    if probe.accelerators and requested not in probe.accelerators:
        raise PaletteError(
            ErrorCode.ACCELERATION_UNAVAILABLE,
            f"this QEMU build does not offer the {requested!r} accelerator",
            details={"requested": requested, "available": probe.accelerators},
        )
    validate_target(profile)
    if requested == "tcg" and not profile.qemu.allow_accelerator_fallback:
        # Reaching here means the profile itself asked for TCG. That is allowed for
        # trusted-image debugging only, and never as a fallback from whpx/kvm.
        logger.warning(
            "profile %s selects TCG explicitly; QEMU provides no guest isolation "
            "guarantee for that configuration",
            profile.profile_id,
        )
    return requested


def resolve_firmware(profile: Profile, binary: str) -> Profile:
    """Resolve architecture-specific firmware without changing trusted configuration."""
    firmware = profile.qemu.firmware
    for configured in (firmware.code, firmware.vars):
        if configured is None:
            continue
        name = Path(configured).name.lower()
        arm = "aavmf" in name or "aarch64" in name or name == "edk2-arm-vars.fd"
        x86 = "ovmf" in name or "x86_64" in name or "i386" in name
        if (profile.qemu.arch == "aarch64" and x86) or (profile.qemu.arch == "x86_64" and arm):
            raise PaletteError(
                ErrorCode.IMAGE_INVALID, "firmware does not match guest architecture"
            )
        if not Path(configured).is_file():
            raise PaletteError(ErrorCode.IMAGE_MISSING, f"firmware file is missing: {configured}")
    if profile.qemu.arch != "aarch64":
        return profile
    directories = [
        Path(binary).resolve().parent.parent / "share" / "qemu",
        Path(binary).resolve().parent / "share",
        Path("/opt/homebrew/share/qemu"),
        Path("/usr/local/share/qemu"),
        Path("/usr/share/qemu"),
        Path("/usr/share/AAVMF"),
    ]
    resolved: dict[str, str] = {}
    for key, configured, names in (
        ("code", firmware.code, ("edk2-aarch64-code.fd", "AAVMF_CODE.fd")),
        ("vars", firmware.vars, ("edk2-arm-vars.fd", "AAVMF_VARS.fd")),
    ):
        path = (
            Path(configured)
            if configured
            else next(
                (
                    directory / name
                    for directory in directories
                    for name in names
                    if (directory / name).is_file()
                ),
                None,
            )
        )
        if path is None or not path.is_file():
            raise PaletteError(
                ErrorCode.IMAGE_MISSING,
                f"AArch64 UEFI {key} is missing; set qemu.firmware.{key} in the profile",
            )
        resolved[key] = str(path.resolve())
    return profile.model_copy(
        update={
            "qemu": profile.qemu.model_copy(
                update={"firmware": firmware.model_copy(update=resolved)}
            )
        }
    )


@dataclass(frozen=True, slots=True)
class ExtraVolume:
    """A host file a caller asked to attach, beyond the instance's own overlay.

    ``cdrom`` media is attached through ``virtio-scsi-pci`` with a ``scsi-cd`` device, which
    is the recipe QEMU's own manual shows for a bootable optical drive. We add the SCSI
    controller ourselves rather than relying on whatever ``-nodefaults`` left behind, so an
    ISO works regardless of the machine's default devices. ``disk`` media uses the same
    ``virtio-blk-pci`` shape as the instance disk.
    """

    path: str | None
    media: Literal["disk", "cdrom"] = "disk"
    format: str = "raw"
    read_only: bool = True
    boot_index: int | None = None
    device_id: str | None = None
    interface: str | None = None
    use_overlay: bool = False


@dataclass(frozen=True, slots=True)
class LaunchOverrides:
    """Host material a caller supplied for one start, over and above the profile.

    The service validates and authorises each field before it gets here; this object exists
    so that the manager, the launcher and the manifest all describe the same set of
    overrides, and so that a reset can rebuild the instance the caller asked for rather than
    silently reverting to the profile.
    """

    image: Path | None = None
    image_format: str | None = None
    volumes: tuple[ExtraVolume, ...] = ()
    qemu_args: tuple[str, ...] = ()
    tools_iso: Path | None = None
    attach_primary: bool = True
    create_overlay: bool = True

    @property
    def is_empty(self) -> bool:
        return not (
            self.image
            or self.volumes
            or self.qemu_args
            or self.tools_iso
            or not self.attach_primary
            or not self.create_overlay
        )

    def describe(self) -> list[str]:
        """A shape summary for the audit trail: counts and kinds, never paths."""
        parts: list[str] = []
        if self.image:
            parts.append("image")
        if self.volumes:
            parts.append(f"{len(self.volumes)} volume(s)")
        if self.qemu_args:
            parts.append(f"{len(self.qemu_args)} qemu arg(s)")
        if self.tools_iso:
            parts.append("tools CD-ROM")
        return parts


@dataclass(slots=True)
class LaunchPlan:
    """A fully resolved, inspectable command. Tests assert on this rather than on a shell."""

    argv: list[str]
    env: dict[str, str]
    cwd: Path
    accelerator: str
    notes: list[str] = field(default_factory=list)
    memory_limit_bytes: int | None = None
    max_runtime_seconds: float | None = None
    host_path_config: bytes | None = None
    guest_agent_endpoint: str | None = None
    webdav_endpoint: str | None = None
    session_endpoint: str | None = None
    clipboard_enabled: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "argv": self.argv,
            "cwd": str(self.cwd),
            "accelerator": self.accelerator,
            "env_keys": sorted(self.env),
            "notes": self.notes,
        }


#: Tokens that create a second way to drive, watch or talk to the guest. Palette adds its own
#: QMP channel deliberately; anything else coming from a profile or a caller breaks the
#: single-writer assumption, which the service cannot detect afterwards. Only extra and
#: caller-supplied arguments are scanned, so Palette's own `-chardev stdio,id=qmp` is not a
#: false positive here.
_EXTRA_CHANNEL_TOKENS = ("-mon", "-monitor", "monitor=", "-chardev", "vnc", "spice", "telnet")


def _flag_extra_channels(args: Sequence[str], notes: list[str]) -> None:
    lowered = " ".join(args).lower()
    matched = sorted({token for token in _EXTRA_CHANNEL_TOKENS if token in lowered})
    if not matched:
        return
    notes.append(
        f"extra arguments add a monitor or display channel ({', '.join(matched)}); the "
        "single-writer guarantee no longer holds for this instance"
    )
    logger.warning(
        "QEMU arguments introduce an additional control or display channel (%s); Palette "
        "cannot enforce single-writer behaviour once one exists",
        ", ".join(matched),
    )


def _windows_drive_config(
    path: str, options: dict[str, str], sections: list[str], *, force: bool = False
) -> bool:
    """Bypass QEMU's UTF-8 argv/JSON -> ANSI file API mismatch on Windows.

    Its legacy config reader preserves native-codepage bytes. Keep this limited to
    non-ASCII host paths; formats, read-only flags and device attachment stay explicit.
    Config values are literal (including backslashes and commas), not JSON strings.
    """
    if not IS_WINDOWS or (path.isascii() and not force):
        return False
    values = {"file": path, **options}
    for value in values.values():
        if any(char in value for char in ('"', "\r", "\n", "\0")):
            raise PaletteError(ErrorCode.STARTUP_FAILED, "invalid QEMU host path config value")
    section = "[drive]\n" + "".join(f'  {key} = "{value}"\n' for key, value in values.items())
    try:
        encoded = section.encode(locale.getencoding())
    except UnicodeEncodeError as exc:
        raise PaletteError(
            ErrorCode.STARTUP_FAILED,
            "this QEMU build requires host paths representable in the Windows system code page",
            details={"path": path},
        ) from exc
    # QEMU's readconfig parser has a 1024-byte line buffer; do not allow truncation.
    if any(len(line) >= 1023 for line in encoded.splitlines()):
        raise PaletteError(ErrorCode.STARTUP_FAILED, "QEMU host path config value is too long")
    sections.append(section)
    return True


def _extra_volume_args(
    volumes: Sequence[ExtraVolume], notes: list[str], config_sections: list[str], profile: Profile
) -> list[str]:
    """Attach caller-supplied disks and CD-ROMs behind explicit blockdev nodes."""
    if not volumes:
        return []
    argv: list[str] = []
    if any(
        volume.interface == "scsi"
        or (volume.media == "cdrom" and not volume.device_id and volume.interface is None)
        for volume in volumes
    ):
        # Added unconditionally when an optical drive is requested: the controller must not
        # depend on which default devices the machine happens to keep.
        argv += ["-device", "virtio-scsi-pci,id=scsi0"]
    ide_index = 0
    for index, volume in enumerate(volumes):
        node = volume.device_id or f"extra{index}"
        native_path = bool(volume.path) and _windows_drive_config(
            volume.path or "",
            {
                "id": node,
                "if": "none",
                "format": volume.format,
                "readonly": "on" if volume.read_only else "off",
            },
            config_sections,
        )
        if volume.path and not native_path:
            argv += [
                "-blockdev",
                json.dumps(
                    {
                        "driver": "file",
                        "filename": volume.path,
                        "node-name": f"{node}-file",
                        "read-only": volume.read_only,
                    },
                    separators=(",", ":"),
                ),
                "-blockdev",
                json.dumps(
                    {
                        "driver": volume.format,
                        "file": f"{node}-file",
                        "node-name": node,
                        "read-only": volume.read_only,
                    },
                    separators=(",", ":"),
                ),
            ]
        interface = volume.interface or (
            "usb"
            if volume.device_id and volume.media == "cdrom"
            else "scsi"
            if volume.media == "cdrom"
            else "virtio"
        )
        boot = f",bootindex={volume.boot_index}" if volume.boot_index is not None else ""
        backend = f",drive={node}" if volume.path else ""
        identity = f",id={node}" if volume.device_id else ""
        kind = "cd" if volume.media == "cdrom" else "hd"
        if interface == "usb":
            argv += ["-device", f"usb-bot,id={node}-usb,bus=xhci.0"]
            argv += ["-device", f"scsi-{kind}{identity}{backend},bus={node}-usb.0{boot}"]
        elif interface == "scsi":
            argv += ["-device", f"scsi-{kind}{identity}{backend},bus=scsi0.0{boot}"]
        elif interface == "ide":
            q35 = profile.qemu.machine.split(",")[0].startswith(("q35", "pc-q35"))
            if profile.qemu.arch != "x86_64" or ide_index >= (6 if q35 else 4):
                raise PaletteError(ErrorCode.INVALID_ARGUMENT, "IDE 设备数量或机器类型不受支持")
            bus, unit = (ide_index, 0) if q35 else (ide_index // 2, ide_index % 2)
            argv += ["-device", f"ide-{kind}{identity}{backend},bus=ide.{bus},unit={unit}{boot}"]
            ide_index += 1
        elif interface == "nvme" and volume.media == "disk":
            argv += ["-device", f"nvme{identity}{backend},serial={node}{boot}"]
        elif interface == "virtio" and volume.media == "disk":
            argv += ["-device", f"virtio-blk-pci,drive={node}{identity}{boot}"]
        else:
            raise PaletteError(ErrorCode.INVALID_ARGUMENT, "不支持的磁盘接口或介质类型")
        notes.append(f"attached a caller-supplied {volume.media}")
    return argv


def build_launch_plan(
    *,
    profile: Profile,
    binary: str,
    instance_id: str,
    vm_id: str,
    overlay_path: Path,
    workdir: Path,
    hardware_vars_path: Path | None = None,
    accelerator: str | None = None,
    start_paused: bool = True,
    managed_console: bool = False,
    clipboard_available: bool = True,
    tools_iso: Path | None = None,
    extra_volumes: Sequence[ExtraVolume] = (),
    caller_args: Sequence[str] = (),
    attach_primary: bool = True,
) -> LaunchPlan:
    """Compose QEMU's arguments from the profile plus anything the caller supplied.

    ``-display none`` is not decoration: it removes every window, VNC and SPICE entry point,
    each of which would be a second, unauthenticated input channel. The network comes from
    the profile's mode, and ``caller_args`` are appended last so a caller can override or add
    to everything above — including, deliberately, the guarantees this function otherwise
    provides. See ``_flag_extra_channels``.
    """
    qemu = profile.qemu
    devices = profile.devices
    resources = profile.resources
    network = profile.network
    notes: list[str] = []
    config_sections: list[str] = []

    argv: list[str] = [binary, *qemu.argv_prefix]
    argv += ["-name", f"palette-{vm_id}-{instance_id}"]
    argv += ["-machine", qemu.machine]
    argv += ["-accel", accelerator or qemu.accelerator]
    argv += ["-m", str(resources.memory_mib)]
    argv += ["-smp", str(resources.vcpus)]
    if qemu.cpu:
        argv += ["-cpu", qemu.cpu]
    argv += ["-nodefaults", "-display", "none"]
    if managed_console:
        argv += ["-vnc", "127.0.0.1:0,to=99,password=on,share=force-shared"]
    clipboard_enabled = managed_console and profile.guest_desktop.clipboard and clipboard_available
    endpoints: dict[str, str] = {}
    if (
        profile.guest_agent.enabled
        or clipboard_enabled
        or profile.guest_desktop.shared_folders
        or profile.guest_desktop.resize
    ):
        argv += ["-device", "virtio-serial-pci,id=palette-serial"]
    for key, enabled, port in (
        ("qga", profile.guest_agent.enabled, "org.qemu.guest_agent.0"),
        ("webdav", profile.guest_desktop.shared_folders, "org.spice-space.webdav.0"),
        ("session", profile.guest_desktop.resize, "org.palette.desktop.0"),
    ):
        if not enabled:
            continue
        if IS_WINDOWS:
            name = f"palette-{key}-{uuid.uuid4().hex}"
            endpoints[key] = rf"\\.\pipe\{name}"
            backend = f"pipe,id=palette-{key},path={name}"
        else:
            endpoints[key] = str(workdir / f"{key}.sock")
            backend = f"socket,id=palette-{key},path={key}.sock,server=on,wait=off"
        argv += [
            "-chardev",
            backend,
            "-device",
            f"virtserialport,id=palette-{key}-port,bus=palette-serial.0,"
            f"chardev=palette-{key},name={port}",
        ]
    if clipboard_enabled:
        argv += [
            "-chardev",
            "qemu-vdagent,id=palette-vdagent,clipboard=on,mouse=off",
            "-device",
            "virtserialport,id=palette-vdagent-port,bus=palette-serial.0,"
            "chardev=palette-vdagent,name=com.redhat.spice.0",
        ]
    elif profile.guest_desktop.clipboard:
        notes.append("clipboard enhancement unavailable; base display and input remain available")
    nic = network.qemu_nic_argument()
    if qemu.arch == "aarch64" and network.attaches_nic and "model=" not in nic:
        nic += ",model=virtio-net-pci"
    argv += ["-nic", nic]
    if network.mode == "offline":
        notes.append("no network device attached")
    elif network.mode == "restricted-lab":
        notes.append("user-mode networking with restrict=on: the guest cannot route past it")
    elif network.mode == "nat":
        notes.append(
            "user-mode NAT: the guest has the same outbound reach as the host, and there is "
            "no egress policy"
        )
    if network.hostfwd:
        notes.append(f"{len(network.hostfwd)} host port forward(s) configured")

    if qemu.firmware.code:
        native_firmware = not qemu.firmware.code.isascii() or (
            hardware_vars_path is not None and not str(hardware_vars_path).isascii()
        )
        if not _windows_drive_config(
            qemu.firmware.code,
            {"if": "pflash", "format": "raw", "readonly": "on"},
            config_sections,
            force=native_firmware,
        ):
            argv += ["-drive", f"if=pflash,format=raw,readonly=on,file={qemu.firmware.code}"]
        # A writable variable store is per-instance state. Keep both pflash drives in
        # the same input source so readconfig cannot reverse their implicit unit order.
        if (
            qemu.firmware.vars
            and hardware_vars_path is not None
            and not _windows_drive_config(
                str(hardware_vars_path),
                {"if": "pflash", "format": "raw"},
                config_sections,
                force=native_firmware,
            )
        ):
            argv += ["-drive", f"if=pflash,format=raw,file={hardware_vars_path}"]

    # The display device must exist before the input devices that bind to it.
    display = f"{devices.display_model},id={devices.display_id}"
    if devices.default_resolution:
        display += f",xres={profile.display.width},yres={profile.display.height}"
    argv += ["-device", display]
    if profile.audio.enabled:
        if profile.audio.backend is None:
            raise PaletteError(ErrorCode.UNSUPPORTED_CAPABILITY, "宿主音频后端尚未确认")
        argv += [
            "-audiodev",
            f"{profile.audio.backend},id=audio0",
            "-device",
            "intel-hda,id=sound0",
            "-device",
            "hda-output,bus=sound0.0,audiodev=audio0",
        ]
    argv += ["-device", "qemu-xhci,id=xhci"]
    argv += [
        "-device",
        f"usb-kbd,id={devices.keyboard_id},bus=xhci.0,display={devices.display_id}",
    ]
    argv += [
        "-device",
        f"usb-tablet,id={devices.pointer_id},bus=xhci.0,display={devices.display_id},"
        f"head={devices.head}",
    ]

    if attach_primary:
        argv += _block_args(
            overlay_path=overlay_path,
            profile=profile,
            notes=notes,
            config_sections=config_sections,
            boot_index=1 if any(volume.boot_index == 0 for volume in extra_volumes) else 0,
        )
    argv += _extra_volume_args(extra_volumes, notes, config_sections, profile)
    if tools_iso is not None:
        options = {
            "id": "palette-tools",
            "if": "none",
            "media": "cdrom",
            "readonly": "on",
            "format": "raw",
        }
        if not _windows_drive_config(str(tools_iso), options, config_sections):
            # QemuOpts doubles literal commas; -drive does not accept JSON.
            argv += [
                "-drive",
                ",".join(
                    f"{key}={value.replace(',', ',,')}"
                    for key, value in {**options, "file": str(tools_iso)}.items()
                ),
            ]
        argv += [
            "-device",
            "usb-bot,id=palette-tools-usb,bus=xhci.0",
            "-device",
            "scsi-cd,id=palette-tools-cd,drive=palette-tools,bus=palette-tools-usb.0",
        ]
    if config_sections:
        # Relative to cwd so a non-ASCII runtime directory is never sent via argv.
        argv += ["-readconfig", "qemu-host-paths.conf"]
        notes.append("Windows host paths loaded through a native-codepage drive config")

    # QMP over the child's own stdin/stdout, never a socket. ``signal=off`` keeps QEMU
    # from installing host signal handlers around our channel.
    argv += ["-chardev", "stdio,id=qmp,signal=off", "-mon", "chardev=qmp,mode=control"]
    argv += list(qemu.extra_args)
    argv += list(caller_args)
    if caller_args:
        notes.append(f"{len(caller_args)} caller-supplied QEMU argument(s) appended")
    _flag_extra_channels([*qemu.extra_args, *caller_args], notes)
    if start_paused:
        argv += ["-S"]
        notes.append("started paused; the manager resumes it once QMP is negotiated")

    env = build_child_env(audio=profile.audio.enabled)
    return LaunchPlan(
        argv=argv,
        env=env,
        cwd=workdir,
        accelerator=accelerator or qemu.accelerator,
        notes=notes,
        memory_limit_bytes=(
            resources.process_memory_limit_mib * 1024 * 1024
            if resources.process_memory_limit_mib
            else None
        ),
        max_runtime_seconds=resources.process_max_runtime_seconds,
        host_path_config=(
            "\n".join(config_sections).encode(locale.getencoding()) if config_sections else None
        ),
        guest_agent_endpoint=endpoints.get("qga"),
        webdav_endpoint=endpoints.get("webdav"),
        session_endpoint=endpoints.get("session"),
        clipboard_enabled=clipboard_enabled,
    )


def _block_args(
    *,
    overlay_path: Path,
    profile: Profile,
    notes: list[str],
    config_sections: list[str],
    boot_index: int = 0,
) -> list[str]:
    """Explicit blockdev graph: file node and format node are separate.

    The format is stated, never probed from the file's extension, so a renamed or
    swapped image cannot change how QEMU interprets the backing chain.
    """
    device = f"virtio-blk-pci,drive=disk,bootindex={boot_index}"
    if _windows_drive_config(
        str(overlay_path),
        {"id": "disk", "if": "none", "format": "qcow2", "discard": "unmap"},
        config_sections,
    ):
        return ["-device", device]
    # Compact JSON preserves paths without shell or QemuOpts escaping.
    file_node = json.dumps(
        {
            "driver": "file",
            "filename": str(overlay_path),
            "node-name": "disk-file",
            "auto-read-only": True,
            "discard": "unmap",
        },
        separators=(",", ":"),
    )
    format_node = json.dumps(
        {
            "driver": "qcow2",
            "file": "disk-file",
            "node-name": "disk",
            "read-only": False,
        },
        separators=(",", ":"),
    )
    return [
        "-blockdev",
        file_node,
        "-blockdev",
        format_node,
        "-device",
        device,
    ]


async def spawn_qemu(
    plan: LaunchPlan, *, on_spawn: Callable[[ManagedProcess], None] | None = None
) -> ManagedProcess:
    """Start QEMU detached from Palette's own standard streams.

    ``stdin``/``stdout`` are the QMP pipe; MCP's stdout must stay clean, so QEMU's
    diagnostics go to a separate ``stderr`` pipe that the manager consumes continuously
    (an unread stderr pipe would eventually block the guest).
    """
    task = asyncio.create_task(_spawn_qemu(plan, on_spawn=on_spawn))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        process = await finish_cleanup(task)
        await finish_cleanup(asyncio.create_task(_dispose_spawned(process)))
        raise


async def _dispose_spawned(process: ManagedProcess) -> None:
    """Before ownership reaches the manager, cleanup owns and drains every output pipe."""
    if process.stdin is not None:
        process.stdin.close()
    process.kill()

    async def drain(reader: asyncio.StreamReader | None) -> None:
        if reader is not None:
            while await reader.read(65536):
                pass

    try:
        async with asyncio.timeout(10):
            await asyncio.gather(process.wait(), drain(process.stdout), drain(process.stderr))
    except TimeoutError as exc:
        raise PaletteError(
            ErrorCode.TIMEOUT, "spawned process cleanup could not be confirmed"
        ) from exc


async def _spawn_qemu(
    plan: LaunchPlan, *, on_spawn: Callable[[ManagedProcess], None] | None = None
) -> ManagedProcess:
    if plan.host_path_config is not None:
        try:
            (plan.cwd / "qemu-host-paths.conf").write_bytes(plan.host_path_config)
        except OSError as exc:
            raise PaletteError(
                ErrorCode.STARTUP_FAILED, "QEMU host path config could not be written"
            ) from exc
    try:
        if not IS_WINDOWS:
            return await _spawn_supervisor(plan, on_spawn=on_spawn)
        process = await asyncio.create_subprocess_exec(
            *plan.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(plan.cwd),
            env=plan.env,
            limit=STREAM_LIMIT,
            close_fds=True,
            creationflags=CREATION_FLAGS,
            start_new_session=START_NEW_SESSION,
        )
        if on_spawn is not None:
            on_spawn(process)
        return process
    except FileNotFoundError as exc:
        raise PaletteError(
            ErrorCode.QEMU_NOT_FOUND, "the configured QEMU executable could not be started"
        ) from exc
    except OSError as exc:
        raise PaletteError(
            ErrorCode.STARTUP_FAILED, f"QEMU could not be started: {type(exc).__name__}"
        ) from exc


def spawn_supervised_child(argv: Sequence[str]) -> subprocess.Popen[bytes]:
    """Called only by the POSIX supervisor; all child spawning stays in this module."""
    return subprocess.Popen(  # noqa: S603
        list(argv),
        stdin=0,
        stdout=1,
        stderr=2,
        env=build_child_env(audio="-audiodev" in argv),
        close_fds=True,
        start_new_session=True,
    )


async def _spawn_supervisor(
    plan: LaunchPlan, *, on_spawn: Callable[[ManagedProcess], None] | None = None
) -> SupervisedProcess:
    live_read, live_write = os.pipe()
    status_read, status_write = os.pipe()
    supervisor: asyncio.subprocess.Process | None = None
    handle: SupervisedProcess | None = None
    transport: asyncio.ReadTransport | None = None
    try:
        reader = asyncio.StreamReader(limit=8192)
        protocol = asyncio.StreamReaderProtocol(reader)
        pipe = os.fdopen(status_read, "rb", buffering=0)
        status_read = -1
        try:
            transport, _ = await asyncio.get_running_loop().connect_read_pipe(
                lambda: protocol, pipe
            )
        except BaseException:
            pipe.close()
            raise
        supervisor = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-m",
            "palette.vm.supervisor",
            "--liveness-fd",
            str(live_read),
            "--status-fd",
            str(status_write),
            "--memory-limit",
            str(plan.memory_limit_bytes or 0),
            "--runtime-limit",
            str(plan.max_runtime_seconds or 0),
            "--",
            *plan.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(plan.cwd),
            env=plan.env,
            pass_fds=(live_read, status_write),
            close_fds=True,
            start_new_session=True,
            limit=STREAM_LIMIT,
        )
        handle = SupervisedProcess(supervisor, reader, transport, live_write)
        live_write = -1
        if on_spawn is not None:
            on_spawn(handle)
        os.close(live_read)
        live_read = -1
        os.close(status_write)
        status_write = -1
        await asyncio.wait_for(handle.ready(), timeout=15)
        return handle
    except BaseException:
        if handle is not None:
            handle.close()
            await finish_cleanup(asyncio.create_task(_dispose_spawned(handle)))
        elif supervisor is not None:
            if live_write >= 0:
                os.close(live_write)
                live_write = -1
            communication = asyncio.create_task(supervisor.communicate())
            await finish_cleanup(asyncio.create_task(_stop_tool(supervisor, communication)))
        raise
    finally:
        if handle is None and transport is not None:
            transport.close()
        for fd in (live_read, live_write, status_read, status_write):
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)


async def _stop_tool(
    process: asyncio.subprocess.Process,
    communication: asyncio.Task[tuple[bytes, bytes]],
) -> None:
    try:
        if not await asyncio.to_thread(terminate_process_group, process.pid):
            raise PaletteError(ErrorCode.TIMEOUT, "helper process tree exit could not be confirmed")
        async with asyncio.timeout(5):
            await asyncio.shield(communication)
    except BaseException as exc:
        communication.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await communication
        if isinstance(exc, TimeoutError):
            raise PaletteError(ErrorCode.TIMEOUT, "helper output pipe cleanup timed out") from exc
        raise


async def run_tool(
    argv: list[str],
    *,
    timeout: float = 120.0,
    cwd: Path | None = None,
) -> tuple[int, str, str]:
    """Run a QEMU-family helper (``qemu-img``) with a fixed argument vector."""
    spawn = asyncio.create_task(
        asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd) if cwd else None,
            env=build_child_env(),
            creationflags=CREATION_FLAGS,
            start_new_session=START_NEW_SESSION,
        )
    )
    try:
        process = await asyncio.shield(spawn)
    except asyncio.CancelledError:
        process = await finish_cleanup(spawn)
        communication = asyncio.create_task(process.communicate())
        await finish_cleanup(asyncio.create_task(_stop_tool(process, communication)))
        raise
    except FileNotFoundError as exc:
        raise PaletteError(
            ErrorCode.QEMU_NOT_FOUND, f"helper executable {argv[0]!r} was not found"
        ) from exc
    communication = asyncio.create_task(process.communicate())
    try:
        stdout, stderr = await asyncio.wait_for(asyncio.shield(communication), timeout=timeout)
    except (TimeoutError, asyncio.CancelledError) as exc:
        await finish_cleanup(asyncio.create_task(_stop_tool(process, communication)))
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise PaletteError(ErrorCode.TIMEOUT, "helper executable timed out") from None
    return (
        process.returncode or 0,
        stdout.decode("utf-8", "replace"),
        stderr.decode("utf-8", "replace"),
    )
