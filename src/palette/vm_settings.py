"""Typed operator hardware settings and bounded, host-specific capability probes."""

from __future__ import annotations

import asyncio
import os
import re
import sys
from typing import Any, Literal

import psutil
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .config import HostConfig, Profile
from .errors import ErrorCode, PaletteError
from .vm.launcher import locate_binary, run_tool


class SettingsModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class DisplaySettings(SettingsModel):
    model: str = Field(max_length=128)
    width: int = Field(ge=2, le=8192)
    height: int = Field(ge=2, le=8192)
    default_resolution: bool = False

    @model_validator(mode="after")
    def bounded_surface(self) -> DisplaySettings:
        if self.width * self.height > 16_777_216:
            raise ValueError("display exceeds the framebuffer pixel limit")
        return self


class PortForward(SettingsModel):
    protocol: Literal["tcp", "udp"] = "tcp"
    host_port: int = Field(ge=1, le=65535)
    guest_port: int = Field(ge=1, le=65535)


class NetworkSettings(SettingsModel):
    model: Literal["virtio-net-pci", "e1000", "rtl8139"] | None = None
    mac: str | None = Field(default=None, max_length=17)
    forwards: list[PortForward] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def valid_network(self) -> NetworkSettings:
        if self.mac is not None:
            if not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", self.mac):
                raise ValueError("MAC address must have six hexadecimal octets")
            if int(self.mac[:2], 16) & 1 or self.mac.lower() == "00:00:00:00:00:00":
                raise ValueError("MAC address must be a nonzero unicast address")
        ports = [(entry.protocol, entry.host_port) for entry in self.forwards]
        if len(ports) != len(set(ports)):
            raise ValueError("duplicate host port forward")
        return self


class DriveSettings(SettingsModel):
    id: str = Field(pattern=r"^drive-[a-z0-9_-]{1,16}$")
    media: Literal["disk", "cdrom"]
    path: str | None = Field(default=None, min_length=1, max_length=4096)
    format: Literal["raw", "qcow2"] = "raw"
    read_only: bool = True
    attached: bool = True
    size_gib: int | None = Field(default=None, ge=1, le=4096)
    interface: Literal["virtio", "ide", "scsi", "nvme", "usb"] | None = None
    import_copy: bool = False

    @model_validator(mode="after")
    def valid_media(self) -> DriveSettings:
        if self.size_gib is not None:
            if self.media != "disk" or self.path is not None:
                raise ValueError("空白磁盘不能指定外部镜像或光驱类型")
            if "format" not in self.model_fields_set:
                self.format = "qcow2"
            if "read_only" not in self.model_fields_set:
                self.read_only = False
        if self.import_copy and (self.media != "disk" or not self.path):
            raise ValueError("导入磁盘需要源镜像文件")
        if self.import_copy:
            if "format" not in self.model_fields_set:
                self.format = "qcow2"
            if "read_only" not in self.model_fields_set:
                self.read_only = False
        if self.media == "cdrom" and self.interface in {"virtio", "nvme"}:
            raise ValueError("此接口不支持光驱")
        if self.media == "disk" and not self.path and self.size_gib is None:
            raise ValueError("附加磁盘需要选择镜像文件")
        if self.media == "cdrom" and (self.format != "raw" or not self.read_only):
            raise ValueError("光驱只支持只读 ISO")
        return self


class HardwareSettings(SettingsModel):
    display: DisplaySettings
    network: NetworkSettings = Field(default_factory=NetworkSettings)
    audio_enabled: bool = False
    iso_path: str | None = Field(default=None, min_length=1, max_length=4096)
    boot_order: Literal["auto", "disk", "cdrom"] = "auto"
    boot_drive_id: str | None = Field(default=None, pattern=r"^drive-[a-z0-9_-]{1,16}$")
    drives: list[DriveSettings] = Field(default_factory=list, max_length=9)

    @model_validator(mode="after")
    def unique_drives(self) -> HardwareSettings:
        if len({drive.id for drive in self.drives}) != len(self.drives):
            raise ValueError("设备与驱动器 ID 不得重复")
        return self


def settings_from_profile(profile: Profile, iso_path: str | None = None) -> HardwareSettings:
    return HardwareSettings(
        display=DisplaySettings(
            model=profile.devices.display_model,
            width=profile.display.width,
            height=profile.display.height,
            default_resolution=profile.devices.default_resolution,
        ),
        network=NetworkSettings(model=profile.network.model, mac=profile.network.mac),
        audio_enabled=profile.audio.enabled,
        drives=[DriveSettings(id="drive-optical1", media="cdrom", path=iso_path)]
        if iso_path
        else [],
    )


def normalize_optical(settings: HardwareSettings, *, attached: bool = True) -> HardwareSettings:
    """Migrate persisted single-ISO settings into the ordinary drive list."""
    if not settings.iso_path:
        return settings
    index = 1
    while f"drive-optical{index}" in {drive.id for drive in settings.drives}:
        index += 1
    drive = DriveSettings(
        id=f"drive-optical{index}", media="cdrom", path=settings.iso_path, attached=attached
    )
    return settings.model_copy(update={"iso_path": None, "drives": [drive, *settings.drives]})


def apply_settings(profile: Profile, settings: HardwareSettings) -> Profile:
    data = profile.model_dump()
    data["devices"].update(
        display_model=settings.display.model,
        default_resolution=settings.display.default_resolution,
    )
    data["display"].update(width=settings.display.width, height=settings.display.height)
    data["network"].update(model=settings.network.model, mac=settings.network.mac)
    data["audio"].update(enabled=settings.audio_enabled, backend=None)
    return Profile.model_validate(data)


class CapabilityProbe:
    def __init__(self, host: HostConfig) -> None:
        self.host = host
        self.cache: dict[tuple[str, ...], dict[str, Any]] = {}
        self._pending: dict[tuple[str, ...], asyncio.Task[dict[str, Any]]] = {}

    async def get(self, profile: Profile) -> dict[str, Any]:
        binary = locate_binary(profile, self.host)
        if binary is None:
            return self._empty("未找到 QEMU; 请先配置运行环境")
        key = (
            binary,
            *profile.qemu.argv_prefix,
            profile.qemu.arch,
            profile.qemu.machine,
            profile.devices.display_model,
        )
        if key in self.cache:
            return self.cache[key]
        task = self._pending.get(key)
        if task is None:
            task = asyncio.create_task(self._probe(profile, binary), name="hardware-probe")
            self._pending[key] = task

            def completed(done: asyncio.Task[dict[str, Any]]) -> None:
                self._pending.pop(key, None)
                if not done.cancelled() and done.exception() is None:
                    result = done.result()
                    if result["available"]:
                        self.cache[key] = result

            task.add_done_callback(completed)
        return await asyncio.shield(task)

    async def close(self) -> None:
        tasks = list(self._pending.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._pending.clear()

    @staticmethod
    def _empty(reason: str) -> dict[str, Any]:
        return {
            "available": False,
            "reason": reason,
            "displays": [],
            "network_models": [],
            "drive_interfaces": {"disk": [], "cdrom": []},
            "audio_backend": None,
            "audio_available": False,
            "host": {"vcpus": os.cpu_count(), "memory_mib": psutil.virtual_memory().total // 2**20},
        }

    async def _probe(self, profile: Profile, binary: str) -> dict[str, Any]:
        command = [binary, *profile.qemu.argv_prefix]
        try:
            devices, audio = await asyncio.gather(
                run_tool([*command, "-device", "help"], timeout=10),
                run_tool([*command, "-audiodev", "help"], timeout=10),
            )
            if devices[0] != 0:
                return self._empty("无法读取 QEMU 设备能力")
            names = set(re.findall(r'^\s*name "([^"]+)"', devices[1], re.MULTILINE))
            candidates = (
                ["VGA", "virtio-gpu-pci", "virtio-vga"]
                if profile.qemu.arch == "x86_64"
                else ["ramfb", "virtio-gpu-pci"]
            )
            if profile.devices.display_model not in candidates and not (
                profile.qemu.arch != "x86_64" and profile.devices.display_model == "virtio-vga"
            ):
                candidates.append(profile.devices.display_model)
            displays = []
            for model in candidates:
                if model not in names:
                    continue
                code, out, err = await run_tool([*command, "-device", f"{model},help"], timeout=10)
                if code != 0:
                    continue
                displays.append(
                    {
                        "model": model,
                        "resolution": code == 0 and "xres=" in out + err and "yres=" in out + err,
                    }
                )
            drivers = (
                ("coreaudio",)
                if sys.platform == "darwin"
                else ("dsound",)
                if sys.platform == "win32"
                else ("pipewire", "pa", "alsa")
            )
            offered = set((audio[1] + audio[2]).split()) if audio[0] == 0 else set()
            backend = next((driver for driver in drivers if driver in offered), None)
            result = self._empty("")
            result.update(
                available=True,
                displays=displays,
                network_models=[
                    model for model in ("virtio-net-pci", "e1000", "rtl8139") if model in names
                ],
                audio_backend=backend,
                audio_available=backend is not None and {"intel-hda", "hda-output"} <= names,
                drive_interfaces={
                    media: [
                        interface
                        for interface, devices in {
                            "virtio": {"virtio-blk-pci"} if media == "disk" else set(),
                            "ide": {"ide-hd" if media == "disk" else "ide-cd"}
                            if profile.qemu.arch == "x86_64"
                            and profile.qemu.machine.split(",")[0].startswith(("q35", "pc"))
                            else set(),
                            "scsi": {
                                "virtio-scsi-pci",
                                "scsi-hd" if media == "disk" else "scsi-cd",
                            },
                            "nvme": {"nvme"} if media == "disk" else set(),
                            "usb": {"usb-bot", "scsi-hd" if media == "disk" else "scsi-cd"},
                        }.items()
                        if devices and devices <= names
                    ]
                    for media in ("disk", "cdrom")
                },
            )
            return result
        except (OSError, TimeoutError, PaletteError):
            return self._empty("QEMU 能力检查失败; 请检查运行环境")


async def validate_hardware(
    probe: CapabilityProbe,
    profile: Profile,
    settings: HardwareSettings,
    previous: HardwareSettings | None = None,
) -> dict[str, Any]:
    caps = await probe.get(profile)
    old_drives = {drive.id: drive for drive in previous.drives} if previous else {}
    ide_count = sum(drive.interface == "ide" for drive in settings.drives)
    if ide_count > (6 if profile.qemu.machine.split(",")[0].startswith(("q35", "pc-q35")) else 4):
        raise PaletteError(ErrorCode.INVALID_ARGUMENT, "IDE 驱动器数量超过当前机器限制")
    for drive in settings.drives:
        interface = drive.interface or ("usb" if drive.media == "cdrom" else "virtio")
        old = old_drives.get(drive.id)
        if (
            old is None or (old.interface, old.media) != (drive.interface, drive.media)
        ) and interface not in caps["drive_interfaces"][drive.media]:
            raise PaletteError(
                ErrorCode.UNSUPPORTED_CAPABILITY, "所选磁盘接口不受当前 QEMU 或机器类型支持"
            )
    if previous is None or settings.display != previous.display:
        display = next(
            (item for item in caps["displays"] if item["model"] == settings.display.model), None
        )
        if display is None or (settings.display.default_resolution and not display["resolution"]):
            raise PaletteError(ErrorCode.UNSUPPORTED_CAPABILITY, "所选显卡或默认分辨率不受支持")
    if (
        settings.network.model
        and (previous is None or settings.network.model != previous.network.model)
        and settings.network.model not in caps["network_models"]
    ):
        raise PaletteError(ErrorCode.UNSUPPORTED_CAPABILITY, "所选网卡不受当前 QEMU 支持")
    if settings.audio_enabled and not caps["audio_available"]:
        raise PaletteError(ErrorCode.UNSUPPORTED_CAPABILITY, "当前 QEMU 缺少输出声卡或宿主音频后端")
    return caps
