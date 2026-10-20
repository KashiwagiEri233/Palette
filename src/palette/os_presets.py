"""Guest OS recommendations, independent of host execution profiles and their policy."""

from __future__ import annotations

import asyncio
from typing import Any

from .config import PaletteConfig, Profile
from .errors import PaletteError
from .platforms import host_architecture, native_profile_id, validate_target
from .vm_settings import CapabilityProbe


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


async def list_os_presets(config: PaletteConfig, probe: CapabilityProbe) -> dict[str, Any]:
    profiles = {arch: _profile(config, arch) for arch in ("x86_64", "aarch64")}
    selected_profiles = {arch: profile for arch, profile in profiles.items() if profile is not None}
    capabilities = dict(
        zip(
            selected_profiles,
            await asyncio.gather(*(probe.get(profile) for profile in selected_profiles.values())),
            strict=True,
        )
    )
    presets = []
    for name, label, vcpus, memory, disk in (
        ("windows", "Windows", 4, 8192, 80),
        ("linux", "Linux", 2, 4096, 32),
    ):
        targets = []
        for arch in ("x86_64", "aarch64"):
            profile = profiles[arch]
            preferred = (
                "virtio-vga"
                if arch == "x86_64"
                else "ramfb"
                if name == "windows"
                else "virtio-gpu-pci"
            )
            candidates = [preferred]
            if arch == "x86_64":
                candidates.append("VGA" if name == "windows" else "virtio-gpu-pci")
            caps = capabilities.get(arch)
            supported = (
                {item["model"] for item in caps["displays"]}
                if caps and caps["available"]
                else set()
            )
            display = next((model for model in candidates if model in supported), "")
            if profile is None:
                reason = "没有适用于当前宿主的此架构 Profile"
            elif not caps or not caps["available"]:
                reason = caps["reason"] if caps else "无法检查 QEMU 显卡能力"
            elif not display:
                reason = "当前 QEMU 缺少此预设所需的显卡: " + ", ".join(candidates)
            else:
                reason = (
                    f"当前 QEMU 不支持 {preferred}, 已使用 {display}"
                    if display != preferred
                    else ""
                )
            targets.append(
                {
                    "arch": arch,
                    "profile_id": profile.profile_id if profile else None,
                    "accelerator": profile.qemu.accelerator if profile else None,
                    "requires_emulation": bool(profile and profile.qemu.accelerator == "tcg"),
                    "available": profile is not None and bool(display),
                    "reason": reason,
                    "display_model": display,
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
