"""Hardware capabilities must fail clearly and preserve process boundaries."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from palette.config import AudioConfig, HostConfig, Profile
from palette.errors import PaletteError
from palette.vm.launcher import build_launch_plan
from palette.vm_settings import CapabilityProbe, settings_from_profile, validate_hardware


async def test_capability_probe_is_shared_and_survives_one_cancelled_waiter(
    config, profile, monkeypatch
) -> None:
    probe = CapabilityProbe(config.host)
    entered = asyncio.Event()
    resume = asyncio.Event()
    calls = 0

    async def inspect(*_args):
        nonlocal calls
        calls += 1
        entered.set()
        await resume.wait()
        return {"available": True}

    monkeypatch.setattr(probe, "_probe", inspect)
    first = asyncio.create_task(probe.get(profile))
    await entered.wait()
    second = asyncio.create_task(probe.get(profile))
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    resume.set()
    assert await second == {"available": True}
    assert await probe.get(profile) == {"available": True}
    assert calls == 1
    await probe.close()


async def test_failed_capability_probe_can_be_retried(config, profile, monkeypatch) -> None:
    probe = CapabilityProbe(config.host)
    calls = 0

    async def inspect(*_args):
        nonlocal calls
        calls += 1
        return {"available": calls > 1}

    monkeypatch.setattr(probe, "_probe", inspect)
    assert not (await probe.get(profile))["available"]
    assert (await probe.get(profile))["available"]
    assert calls == 2
    await probe.close()


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


@pytest.mark.parametrize(
    "arch,model", [("x86_64", "VGA"), ("x86_64", "virtio-vga"), ("aarch64", "virtio-gpu-pci")]
)
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


@pytest.mark.parametrize(
    "arch,advertised,properties,expected",
    [
        ("x86_64", True, "xres=<uint32>\nyres=<uint32>", True),
        ("x86_64", True, "xres=<uint32>", False),
        ("x86_64", False, "", None),
        ("aarch64", True, "xres=<uint32>\nyres=<uint32>", None),
    ],
)
async def test_virtio_vga_requires_x86_device_support_and_probes_resolution(
    profile, monkeypatch, arch, advertised, properties, expected
):
    candidate = profile.model_copy(
        update={
            "qemu": profile.qemu.model_copy(update={"arch": arch}),
            "devices": profile.devices.model_copy(update={"display_model": "virtio-vga"}),
        }
    )

    async def run(argv, *, timeout):
        if argv[-2:] == ["-device", "help"]:
            return (
                0,
                'name "VGA"\nname "virtio-gpu-pci"\nname "ramfb"\n'
                + ('name "virtio-vga"' if advertised else ""),
                "",
            )
        if argv[-1] == "virtio-vga,help":
            return 0, properties, ""
        return 0, "", ""

    monkeypatch.setattr("palette.vm_settings.run_tool", run)
    caps = await CapabilityProbe(HostConfig())._probe(candidate, "qemu")
    found = next((item for item in caps["displays"] if item["model"] == "virtio-vga"), None)
    assert (found["resolution"] if found else None) is expected
