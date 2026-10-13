"""Host and guest targets, independent of QEMU process management."""

from __future__ import annotations

import platform
import sys
from typing import TYPE_CHECKING, TypedDict

from .errors import ErrorCode, PaletteError

if TYPE_CHECKING:
    from .config import Profile


def host_architecture() -> str:
    machine = platform.machine().lower()
    return {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)


def native_profile_id() -> str:
    arch = host_architecture()
    if sys.platform == "win32" and arch == "x86_64":
        return "default"
    if sys.platform in {"darwin", "linux"} and arch in {"x86_64", "aarch64"}:
        system = "macos" if sys.platform == "darwin" else "linux"
        return f"{system}-{arch}"
    raise PaletteError(
        ErrorCode.UNSUPPORTED_CAPABILITY,
        "no native profile is available for this host; select an explicit TCG profile",
        details={"platform": sys.platform, "host_arch": arch},
    )


def validate_target(profile: Profile) -> None:
    qemu = profile.qemu
    if qemu.accelerator == "tcg":
        if qemu.cpu == "host":
            raise PaletteError(ErrorCode.INVALID_ARGUMENT, "TCG cannot use cpu=host")
        return
    expected_platform = {"whpx": "win32", "hvf": "darwin", "kvm": "linux"}
    if expected_platform[qemu.accelerator] != sys.platform or qemu.arch != host_architecture():
        raise PaletteError(
            ErrorCode.ACCELERATION_UNAVAILABLE,
            "hardware acceleration requires a matching host platform and guest architecture; "
            "select an explicit TCG profile for software emulation",
            details={
                "platform": sys.platform,
                "host_arch": host_architecture(),
                "guest_arch": qemu.arch,
                "accelerator": qemu.accelerator,
            },
        )


class ExecutionInfo(TypedDict):
    host_arch: str
    guest_arch: str
    accelerator: str
    cross_architecture: bool
    process_protection: str
    memory_enforcement: str
    memory_sample_interval_ms: int | None


def execution_info(profile: Profile) -> ExecutionInfo:
    supervised = sys.platform in {"darwin", "linux"}
    return {
        "host_arch": host_architecture(),
        "guest_arch": profile.qemu.arch,
        "accelerator": profile.qemu.accelerator,
        "cross_architecture": host_architecture() != profile.qemu.arch,
        "process_protection": "posix-supervisor" if supervised else "windows-job-object",
        "memory_enforcement": (
            "disabled"
            if profile.resources.process_memory_limit_mib is None
            else "sampled-rss"
            if supervised
            else "job-object"
        ),
        "memory_sample_interval_ms": 250 if supervised else None,
    }
