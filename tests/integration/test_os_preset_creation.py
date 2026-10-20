"""Persistent creation keeps preset identity separate from actual user settings."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from palette.config import Profile
from palette.desktop import DesktopManager
from palette.errors import ErrorCode, PaletteError
from palette.operator.protocol import DesktopCommand
from palette.runtime import RuntimeOwner
from palette.vm_settings import settings_from_profile


def payload(service, **changes):
    return {
        "name": "Preset VM",
        "os_preset": "windows",
        "profile_id": "default",
        "source": "image",
        "path": service.config.profile("default").images.base,
        "vcpus": 1,
        "memory_mib": 512,
        "disk_gib": 24,
        "network": "offline",
        **changes,
    }


@pytest.mark.parametrize("family", ["windows", "linux", "custom"])
@pytest.mark.parametrize("source", ["image", "iso"])
@pytest.mark.parametrize("arch", ["x86_64", "aarch64"])
async def test_creation_persists_overridden_resources_and_family(
    service, tmp_path, family, source, arch
):
    profile = service.config.profile("default")
    if arch == "aarch64":
        data = profile.model_dump()
        data["qemu"].update(arch="aarch64", machine="virt")
        service.config.profiles["default"] = Profile.model_validate(data)
    source_path = profile.images.base
    if source == "iso":
        iso = tmp_path / "installer.iso"
        iso.write_bytes(b"test media")
        source_path = str(iso)
    desktop = DesktopManager(service)
    document = await desktop.create(
        payload(service, os_preset=family, source=source, path=source_path)
    )
    saved = DesktopManager(service).get(document["vm_id"])
    assert saved.draft.os_preset == family
    assert saved.profile.qemu.arch == arch
    assert saved.profile.resources.vcpus == 1
    assert saved.profile.resources.memory_mib == 512
    assert saved.profile.images.size_gib == 24
    assert saved.profile.qemu.firmware == profile.qemu.firmware
    assert saved.profile.lifecycle == profile.lifecycle


async def test_invalid_new_creation_leaves_no_definition(service, monkeypatch):
    desktop = DesktopManager(service)
    monkeypatch.setattr("palette.desktop.os.cpu_count", lambda: 2)
    monkeypatch.setattr(
        "palette.desktop.psutil.virtual_memory", lambda: SimpleNamespace(total=1024**3)
    )
    for changes in ({"vcpus": 3}, {"memory_mib": 2048}, {"disk_gib": 1}):
        with pytest.raises(PaletteError) as failure:
            await desktop.create(payload(service, **changes))
        assert failure.value.code == ErrorCode.INVALID_ARGUMENT
    assert not desktop.definitions


async def test_old_client_and_old_definition_keep_custom_identity(service):
    desktop = DesktopManager(service)
    old = payload(service)
    old.pop("os_preset")
    document = await desktop.create(old)
    assert document["draft"]["os_preset"] == "custom"
    path = desktop.directory / (document["vm_id"] + ".json")
    document["draft"].pop("os_preset")
    path.write_text(json.dumps(document), encoding="utf-8")
    original = path.read_bytes()
    restored = DesktopManager(service).get(document["vm_id"])
    assert restored.draft.os_preset == "custom"
    assert path.read_bytes() == original


async def test_os_presets_route_is_read_only_and_advertised(service):
    owner = RuntimeOwner(service)
    await owner.start()
    try:
        hello = await owner.route(DesktopCommand(method="hello"))
        assert "os-presets-v1" in hello["features"]
        result = await owner.route(DesktopCommand(method="os_presets.list"))
        assert {item["id"] for item in result["presets"]} == {"windows", "linux"}
        assert not service.manager.known() and not owner.desktop.definitions
    finally:
        await owner.close()


async def test_virtio_vga_persists_and_starts_without_rewriting_custom_defaults(service):
    desktop = DesktopManager(service)
    base = service.config.profile("default")
    settings = settings_from_profile(base)
    settings.display.model = "virtio-vga"
    settings.display.default_resolution = True
    settings.display.width, settings.display.height = 1600, 900
    vm_id = (await desktop.create(payload(service, settings=settings.model_dump())))["vm_id"]
    restored = DesktopManager(service)
    for _ in range(2):
        await restored.start(vm_id)
        instance = service.manager.find(vm_id)
        assert "virtio-vga,id=video0,xres=1600,yres=900" in instance.plan.argv
        assert any("bus=xhci.0,display=video0" in arg for arg in instance.plan.argv)
        await restored.stop(vm_id, force=True)
    assert restored.get(vm_id).draft.settings.display.model == "virtio-vga"
    assert restored.get(vm_id).profile.devices.display_model == "virtio-vga"
    assert base.devices.display_model == "VGA"
