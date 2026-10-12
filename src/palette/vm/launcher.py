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
import logging
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ..config import HostConfig, Profile
from ..errors import ErrorCode, PaletteError

logger = logging.getLogger("palette.vm.launcher")

#: QEMU's stdout carries QMP; the reader needs room for ``query-qmp-schema``.
STREAM_LIMIT = 16 * 1024 * 1024

#: Only these variables are handed to the child. Everything else — including any
#: credential the operator happens to have exported — stays out of the VM's process.
ENV_WHITELIST = (
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "TEMP",
    "TMP",
    "PATH",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "PROCESSOR_IDENTIFIER",
    "LANG",
    "LC_ALL",
)

_VERSION_RE = re.compile(r"QEMU emulator version (\S+)")
_DEVICE_RE = re.compile(r'^\s*name "([^"]+)"', re.MULTILINE)
_BINARY_NAMES = ("qemu-system-x86_64", "qemu-system-x86_64.exe")


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


def build_child_env() -> dict[str, str]:
    return {key: os.environ[key] for key in ENV_WHITELIST if key in os.environ}


#: CREATE_NO_WINDOW keeps QEMU hidden on Windows: the agent operates a VM, never the host
#: desktop, and a stray console window would also steal host focus. On POSIX the process
#: gets its own session instead. Both parameters exist on both platforms and the
#: irrelevant one is ignored, so they can be passed unconditionally and type-check.
CREATION_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
START_NEW_SESSION = sys.platform != "win32"


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
    if candidate.is_file():
        return str(candidate)
    found = shutil.which(configured)
    if found:
        return found
    for name in _BINARY_NAMES:
        found = shutil.which(name)
        if found:
            return found
    for search in host.qemu_search_paths:
        directory = Path(search)
        if not directory.is_dir():
            continue
        for name in _BINARY_NAMES:
            candidate = directory / name
            if candidate.is_file():
                return str(candidate)
        # A qemu release directory nests its executables one level down.
        for nested in sorted(directory.glob("qemu-*")):
            if nested.is_dir():
                for name in _BINARY_NAMES:
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
    if requested == "tcg" and not profile.qemu.allow_accelerator_fallback:
        # Reaching here means the profile itself asked for TCG. That is allowed for
        # trusted-image debugging only, and never as a fallback from whpx/kvm.
        logger.warning(
            "profile %s selects TCG explicitly; QEMU provides no guest isolation "
            "guarantee for that configuration",
            profile.profile_id,
        )
    return requested


@dataclass(frozen=True, slots=True)
class ExtraVolume:
    """A host file a caller asked to attach, beyond the instance's own overlay.

    ``cdrom`` media is attached through ``virtio-scsi-pci`` with a ``scsi-cd`` device, which
    is the recipe QEMU's own manual shows for a bootable optical drive. We add the SCSI
    controller ourselves rather than relying on whatever ``-nodefaults`` left behind, so an
    ISO works regardless of the machine's default devices. ``disk`` media uses the same
    ``virtio-blk-pci`` shape as the instance disk.
    """

    path: str
    media: Literal["disk", "cdrom"] = "disk"
    format: str = "raw"
    read_only: bool = True
    boot_index: int | None = None


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

    @property
    def is_empty(self) -> bool:
        return not (self.image or self.volumes or self.qemu_args)

    def describe(self) -> list[str]:
        """A shape summary for the audit trail: counts and kinds, never paths."""
        parts: list[str] = []
        if self.image:
            parts.append("image")
        if self.volumes:
            parts.append(f"{len(self.volumes)} volume(s)")
        if self.qemu_args:
            parts.append(f"{len(self.qemu_args)} qemu arg(s)")
        return parts


@dataclass(slots=True)
class LaunchPlan:
    """A fully resolved, inspectable command. Tests assert on this rather than on a shell."""

    argv: list[str]
    env: dict[str, str]
    cwd: Path
    accelerator: str
    notes: list[str] = field(default_factory=list)

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


def _extra_volume_args(volumes: Sequence[ExtraVolume], notes: list[str]) -> list[str]:
    """Attach caller-supplied disks and CD-ROMs behind explicit blockdev nodes."""
    if not volumes:
        return []
    argv: list[str] = []
    if any(volume.media == "cdrom" for volume in volumes):
        # Added unconditionally when an optical drive is requested: the controller must not
        # depend on which default devices the machine happens to keep.
        argv += ["-device", "virtio-scsi-pci,id=scsi0"]
    for index, volume in enumerate(volumes):
        node = f"extra{index}"
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
        if volume.media == "cdrom":
            argv += ["-device", f"scsi-cd,drive={node},bus=scsi0.0"]
        else:
            argv += ["-device", f"virtio-blk-pci,drive={node}"]
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
    extra_volumes: Sequence[ExtraVolume] = (),
    caller_args: Sequence[str] = (),
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

    argv: list[str] = [binary, *qemu.argv_prefix]
    argv += ["-name", f"palette-{vm_id}-{instance_id}"]
    argv += ["-machine", qemu.machine]
    argv += ["-accel", accelerator or qemu.accelerator]
    argv += ["-m", str(resources.memory_mib)]
    argv += ["-smp", str(resources.vcpus)]
    if qemu.cpu:
        argv += ["-cpu", qemu.cpu]
    argv += ["-nodefaults", "-display", "none"]
    argv += ["-nic", network.qemu_nic_argument()]
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
        argv += [
            "-drive",
            f"if=pflash,format=raw,readonly=on,file={qemu.firmware.code}",
        ]
        if qemu.firmware.vars and hardware_vars_path is not None:
            # A writable variable store is per-instance state: resetting the VM must not
            # leave the previous boot's boot entries and keys behind.
            argv += ["-drive", f"if=pflash,format=raw,file={hardware_vars_path}"]

    # The display device must exist before the input devices that bind to it.
    argv += ["-device", f"{devices.display_model},id={devices.display_id}"]
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

    argv += _block_args(overlay_path=overlay_path, profile=profile, notes=notes)
    argv += _extra_volume_args(extra_volumes, notes)

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

    return LaunchPlan(
        argv=argv,
        env=build_child_env(),
        cwd=workdir,
        accelerator=accelerator or qemu.accelerator,
        notes=notes,
    )


def _block_args(*, overlay_path: Path, profile: Profile, notes: list[str]) -> list[str]:
    """Explicit blockdev graph: file node and format node are separate.

    The format is stated, never probed from the file's extension, so a renamed or
    swapped image cannot change how QEMU interprets the backing chain.
    """
    # JSON is required for --blockdev; compact separators only (no spaces, no newlines).
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
            "driver": profile.images.format,
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
        "virtio-blk-pci,drive=disk,bootindex=0",
    ]


async def spawn_qemu(plan: LaunchPlan) -> asyncio.subprocess.Process:
    """Start QEMU detached from Palette's own standard streams.

    ``stdin``/``stdout`` are the QMP pipe; MCP's stdout must stay clean, so QEMU's
    diagnostics go to a separate ``stderr`` pipe that the manager consumes continuously
    (an unread stderr pipe would eventually block the guest).
    """
    try:
        return await asyncio.create_subprocess_exec(
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
    except FileNotFoundError as exc:
        raise PaletteError(
            ErrorCode.QEMU_NOT_FOUND, "the configured QEMU executable could not be started"
        ) from exc
    except OSError as exc:
        raise PaletteError(
            ErrorCode.STARTUP_FAILED, f"QEMU could not be started: {type(exc).__name__}"
        ) from exc


async def run_tool(
    argv: list[str],
    *,
    timeout: float = 120.0,
    cwd: Path | None = None,
) -> tuple[int, str, str]:
    """Run a QEMU-family helper (``qemu-img``) with a fixed argument vector."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd) if cwd else None,
            env=build_child_env(),
            creationflags=CREATION_FLAGS,
            start_new_session=START_NEW_SESSION,
        )
    except FileNotFoundError as exc:
        raise PaletteError(
            ErrorCode.QEMU_NOT_FOUND, f"helper executable {argv[0]!r} was not found"
        ) from exc
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        with contextlib.suppress(Exception):
            await process.wait()
        raise PaletteError(ErrorCode.TIMEOUT, "helper executable timed out") from None
    return (
        process.returncode or 0,
        stdout.decode("utf-8", "replace"),
        stderr.decode("utf-8", "replace"),
    )
