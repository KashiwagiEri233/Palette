"""Guest OS recommendations, independent of host execution profiles and their policy."""

from __future__ import annotations

from typing import Any

from .config import PaletteConfig, Profile
from .errors import PaletteError
from .platforms import host_architecture, native_profile_id, validate_target


def _profile(config: PaletteConfig, arch: str) -> Profile | None:
    try:
        preferred = (
            native_profile_id()
            if config.host.default_profile == "auto"
            else config.host.default_profile
        )
    except PaletteError:
        preferred = ""
    candidates = []
    for profile in config.profiles.values():
        if profile.qemu.arch != arch:
            continue
        try:
            validate_target(profile)
        except PaletteError:
            continue
        candidates.append(profile)
    return min(
        candidates,
        key=lambda item: (
            item.profile_id != preferred,
            item.qemu.accelerator == "tcg",
            item.profile_id,
        ),
        default=None,
    )


def list_os_presets(config: PaletteConfig) -> dict[str, Any]:
    presets = []
    for name, label, vcpus, memory, disk in (
        ("windows", "Windows", 4, 8192, 80),
        ("linux", "Linux", 2, 4096, 32),
    ):
        targets = []
        for arch in ("x86_64", "aarch64"):
            profile = _profile(config, arch)
            targets.append(
                {
                    "arch": arch,
                    "profile_id": profile.profile_id if profile else None,
                    "accelerator": profile.qemu.accelerator if profile else None,
                    "requires_emulation": bool(profile and profile.qemu.accelerator == "tcg"),
                    "available": profile is not None,
                    "reason": "" if profile else "没有适用于当前宿主的此架构 Profile",
                    "display_model": "virtio-gpu-pci"
                    if name == "linux"
                    else "VGA"
                    if arch == "x86_64"
                    else "ramfb",
                    "network_model": "e1000"
                    if name == "windows" and arch == "x86_64"
                    else "virtio-net-pci",
                }
            )
        presets.append(
            {
                "id": name,
                "name": label,
                "vcpus": vcpus,
                "memory_mib": memory,
                "disk_gib": disk,
                "targets": targets,
            }
        )
    return {"host_arch": host_architecture(), "presets": presets}
