"""Guest family recommendations never select a foreign host accelerator."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from palette.config import QemuConfig
from palette.os_presets import list_os_presets


@pytest.fixture
def probe():
    return SimpleNamespace(
        get=AsyncMock(
            return_value={
                "available": True,
                "reason": "",
                "displays": [
                    {"model": model, "resolution": model != "ramfb"}
                    for model in ("VGA", "virtio-vga", "virtio-gpu-pci", "ramfb")
                ],
            }
        )
    )


async def test_presets_share_the_host_execution_profile(config, probe):
    catalog = await list_os_presets(config, probe)
    windows, linux = catalog["presets"]
    assert (windows["vcpus"], windows["memory_mib"], windows["disk_gib"]) == (4, 8192, 80)
    assert (linux["vcpus"], linux["memory_mib"], linux["disk_gib"]) == (2, 4096, 32)
    for preset in (windows, linux):
        x86, arm = preset["targets"]
        assert x86["profile_id"] == "default"
        assert x86["requires_emulation"]
        assert not arm["available"] and arm["profile_id"] is None and arm["reason"]
    assert windows["targets"][0]["display_model"] == "virtio-vga"
    assert windows["targets"][1]["display_model"] == ""
    assert linux["targets"][0]["display_model"] == "virtio-vga"


async def test_arm_tcg_is_available_without_silent_native_fallback(config, probe):
    profile = config.profile("default")
    config.profiles["tcg-arm"] = profile.model_copy(
        update={"profile_id": "tcg-arm", "qemu": QemuConfig(arch="aarch64", accelerator="tcg")}
    )
    for preset in (await list_os_presets(config, probe))["presets"]:
        arm = preset["targets"][1]
        assert arm["available"] and arm["profile_id"] == "tcg-arm"
        assert arm["requires_emulation"] and arm["accelerator"] == "tcg"
        assert arm["display_model"] == ("ramfb" if preset["id"] == "windows" else "virtio-gpu-pci")


async def test_foreign_host_profile_is_never_selected(config, probe):
    foreign = "kvm" if sys.platform != "linux" else "whpx"
    original = config.profile("default")
    config.profiles["foreign-arm"] = original.model_copy(
        update={
            "profile_id": "foreign-arm",
            "qemu": QemuConfig(arch="aarch64", accelerator=foreign),
        }
    )
    assert not (await list_os_presets(config, probe))["presets"][0]["targets"][1]["available"]


async def test_native_profile_is_preferred_to_emulation_when_default_is_elsewhere(
    config, monkeypatch, probe
):
    accelerator = {"win32": "whpx", "darwin": "hvf", "linux": "kvm"}[sys.platform]
    monkeypatch.setattr("palette.platforms.host_architecture", lambda: "x86_64")
    original = config.profile("default")
    config = config.model_copy(
        update={"host": config.host.model_copy(update={"default_profile": "not-configured"})}
    )
    config.profiles["native"] = original.model_copy(
        update={"profile_id": "native", "qemu": QemuConfig(arch="x86_64", accelerator=accelerator)}
    )
    target = (await list_os_presets(config, probe))["presets"][1]["targets"][0]
    assert target["profile_id"] == "native" and not target["requires_emulation"]


@pytest.mark.parametrize("models", [("VGA", "virtio-gpu-pci"), ("virtio-gpu-pci",), ()])
async def test_fallback_and_missing_candidates_are_reported(config, probe, models):
    probe.get.return_value["displays"] = [{"model": model} for model in models]
    catalog = await list_os_presets(config, probe)
    for preset, fallback in zip(catalog["presets"], ("VGA", "virtio-gpu-pci"), strict=True):
        target = preset["targets"][0]
        assert target["available"] == (fallback in models)
        assert target["display_model"] == (fallback if fallback in models else "")
        assert target["reason"]
        if target["available"]:
            assert "virtio-vga" in target["reason"] and fallback in target["reason"]
    probe.get.assert_awaited_once_with(config.profile("default"))


async def test_failed_probe_disables_presets_without_changing_profile(config, probe):
    original = config.profile("default").model_dump()
    probe.get.return_value.update(available=False, reason="QEMU unavailable")
    for preset in (await list_os_presets(config, probe))["presets"]:
        target = preset["targets"][0]
        assert not target["available"] and target["reason"] == "QEMU unavailable"
    assert config.profile("default").model_dump() == original
