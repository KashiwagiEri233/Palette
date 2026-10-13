"""Hardware capabilities must fail clearly and preserve process boundaries."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from palette.config import AudioConfig, HostConfig, Profile
from palette.errors import PaletteError
from palette.vm.launcher import build_launch_plan
from palette.vm_settings import CapabilityProbe, settings_from_profile, validate_hardware


async def test_missing_devices_and_audio_are_explained(
    profile: Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = CapabilityProbe(HostConfig())

    async def empty(_: Profile) -> dict[str, Any]:
        return {"displays": [], "network_models": [], "audio_available": False}

    monkeypatch.setattr(probe, "get", empty)
    settings = settings_from_profile(profile)
    with pytest.raises(PaletteError):
        await validate_hardware(probe, profile, settings)
    enabled = settings.model_copy(update={"audio_enabled": True})
    with pytest.raises(PaletteError):
        await validate_hardware(probe, profile, enabled, settings)


@pytest.mark.parametrize("arch,model", [("x86_64", "VGA"), ("aarch64", "virtio-gpu-pci")])
def test_resolution_and_output_only_audio_arguments(
    profile: Profile, tmp_path: Path, arch: str, model: str
) -> None:
    data = profile.model_dump()
    data["qemu"].update(arch=arch, machine="virt" if arch == "aarch64" else "q35")
    data["devices"].update(display_model=model, default_resolution=True)
    data["display"].update(width=1920, height=1080)
    data["audio"].update(enabled=True, backend="coreaudio")
    candidate = Profile.model_validate(data)
    plan = build_launch_plan(
        profile=candidate,
        binary="qemu",
        instance_id="inst_1",
        vm_id="vm_test",
        overlay_path=tmp_path / "disk.qcow2",
        workdir=tmp_path,
    )
    assert f"{model},id=video0,xres=1920,yres=1080" in plan.argv
    assert "hda-output,bus=sound0.0,audiodev=audio0" in plan.argv
    assert not any("duplex" in argument or "hda-input" in argument for argument in plan.argv)


def test_linux_audio_environment_does_not_inherit_credentials(
    profile: Profile, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("palette.vm.launcher.sys.platform", "linux")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/test")
    monkeypatch.setenv("PULSE_SERVER", "unix:/run/user/test/pulse/native")
    monkeypatch.setenv("PALETTE_OPERATOR_TOKEN", "must-not-cross")
    candidate = profile.model_copy(update={"audio": AudioConfig(enabled=True, backend="pa")})
    plan = build_launch_plan(
        profile=candidate,
        binary="qemu",
        instance_id="inst_1",
        vm_id="vm_test",
        overlay_path=tmp_path / "disk.qcow2",
        workdir=tmp_path,
    )
    assert plan.env["XDG_RUNTIME_DIR"] == "/run/user/test"
    assert plan.env["PULSE_SERVER"].startswith("unix:")
    assert "PALETTE_OPERATOR_TOKEN" not in plan.env
    no_audio = build_launch_plan(
        profile=profile,
        binary="qemu",
        instance_id="inst_1",
        vm_id="vm_test",
        overlay_path=tmp_path / "disk.qcow2",
        workdir=tmp_path,
    )
    assert "XDG_RUNTIME_DIR" not in no_audio.env
