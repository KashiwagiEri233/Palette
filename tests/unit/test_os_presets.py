"""Guest family recommendations never select a foreign host accelerator."""

from __future__ import annotations

import sys

from palette.config import QemuConfig
from palette.os_presets import list_os_presets


def test_presets_share_the_host_execution_profile(config):
    catalog = list_os_presets(config)
    windows, linux = catalog["presets"]
    assert (windows["vcpus"], windows["memory_mib"], windows["disk_gib"]) == (4, 8192, 80)
    assert (linux["vcpus"], linux["memory_mib"], linux["disk_gib"]) == (2, 4096, 32)
    for preset in (windows, linux):
        x86, arm = preset["targets"]
        assert x86["profile_id"] == "default"
        assert x86["requires_emulation"]
        assert not arm["available"] and arm["profile_id"] is None and arm["reason"]
    assert windows["targets"][0]["display_model"] == "VGA"
    assert windows["targets"][1]["display_model"] == "ramfb"
    assert linux["targets"][0]["display_model"] == "virtio-gpu-pci"


def test_arm_tcg_is_available_without_silent_native_fallback(config):
    profile = config.profile("default")
    config.profiles["tcg-arm"] = profile.model_copy(
        update={"profile_id": "tcg-arm", "qemu": QemuConfig(arch="aarch64", accelerator="tcg")}
    )
    for preset in list_os_presets(config)["presets"]:
        arm = preset["targets"][1]
        assert arm["available"] and arm["profile_id"] == "tcg-arm"
        assert arm["requires_emulation"] and arm["accelerator"] == "tcg"


def test_foreign_host_profile_is_never_selected(config):
    foreign = "kvm" if sys.platform != "linux" else "whpx"
    original = config.profile("default")
    config.profiles["foreign-arm"] = original.model_copy(
        update={
            "profile_id": "foreign-arm",
            "qemu": QemuConfig(arch="aarch64", accelerator=foreign),
        }
    )
    assert not list_os_presets(config)["presets"][0]["targets"][1]["available"]


def test_native_profile_is_preferred_to_emulation_when_default_is_elsewhere(config, monkeypatch):
    accelerator = {"win32": "whpx", "darwin": "hvf", "linux": "kvm"}[sys.platform]
    monkeypatch.setattr("palette.platforms.host_architecture", lambda: "x86_64")
    original = config.profile("default")
    config = config.model_copy(
        update={"host": config.host.model_copy(update={"default_profile": "not-configured"})}
    )
    config.profiles["native"] = original.model_copy(
        update={"profile_id": "native", "qemu": QemuConfig(arch="x86_64", accelerator=accelerator)}
    )
    target = list_os_presets(config)["presets"][1]["targets"][0]
    assert target["profile_id"] == "native" and not target["requires_emulation"]
