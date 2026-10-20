"""Dedicated tools media preserves system media and respects shared runtime ownership."""

from __future__ import annotations

import hashlib
import json

import pytest

from conftest import read_command_log, write_behavior
from palette.desktop import DesktopManager
from palette.errors import ErrorCode, PaletteError
from palette.tools_media import change_tools_media, checked_tools_iso


@pytest.fixture
def tools_iso(config):
    directory = config.config_root / "images" / "guest-tools"
    directory.mkdir(parents=True)
    path = directory / "palette-tools-windows-x86_64.iso"
    path.write_bytes(b"fake test media".ljust(32768, b"\0"))
    path.with_suffix(".json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "guest_os": "windows",
                "arch": "x86_64",
                "version": "test",
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size_bytes": path.stat().st_size,
            }
        ),
        encoding="utf-8",
    )
    return path


async def create(desktop, *, source="image", path=None, family="windows"):
    profile = desktop.service.config.profile("default")
    return await desktop.create(
        {
            "name": "Windows tools",
            "profile_id": "default",
            "os_preset": family,
            "source": source,
            "path": path or profile.images.base,
            "vcpus": 1,
            "memory_mib": 512,
            "disk_gib": 24,
            "network": "offline",
        }
    )


async def test_cold_mount_persists_without_starting_and_eject_preserves_install_iso(
    service, tools_iso, tmp_path
):
    desktop = DesktopManager(service)
    installer = tmp_path / "system.iso"
    installer.write_bytes(b"system installation fixture")
    document = await create(desktop, source="iso", path=str(installer))
    vm_id = document["vm_id"]
    mounted = await change_tools_media(desktop, vm_id, {"revision": 0}, mount=True)
    assert mounted == {"attached": True, "live": False, "revision": 1}
    assert not service.manager.known()
    restored = DesktopManager(service)
    assert restored.get(vm_id).tools_iso_sha256
    assert restored.get(vm_id).draft.path == str(installer)
    assert restored.get(vm_id).iso_attached
    result = await change_tools_media(restored, vm_id, {"revision": 1}, mount=False)
    assert not result["attached"]
    assert restored.get(vm_id).tools_iso_sha256 is None
    assert restored.get(vm_id).iso_attached


async def test_live_mount_and_eject_touch_only_tools_drive(service, tools_iso, tmp_path):
    desktop = DesktopManager(service)
    installer = tmp_path / "system.iso"
    installer.write_bytes(b"system media")
    document = await create(desktop, source="iso", path=str(installer))
    vm_id = document["vm_id"]
    await desktop.start(vm_id)
    instance = service.manager.find(vm_id)
    revision = desktop.get(vm_id).revision
    request = {"revision": revision, "instance_id": instance.view.instance_id}
    result = await change_tools_media(desktop, vm_id, request, mount=True)
    assert result["live"] and result["attached"]
    records = read_command_log(instance)
    commands = [item for item in records if item["command"] == "blockdev-change-medium"]
    assert len(commands) == 1
    assert commands[0]["arguments"] == {
        "id": "/machine/peripheral/palette-tools-cd",
        "filename": str(tools_iso.resolve()),
        "format": "raw",
        "read-only-mode": "read-only",
    }
    assert instance.overrides.volumes[0].path == str(installer)
    assert desktop.get(vm_id).iso_attached
    await change_tools_media(
        desktop, vm_id, {**request, "revision": result["revision"]}, mount=False
    )
    eject = [item for item in read_command_log(instance) if item["command"] == "eject"]
    assert eject[-1]["arguments"] == {"id": "/machine/peripheral/palette-tools-cd", "force": False}


@pytest.mark.parametrize(
    "failure", ["stale-instance", "stale-revision", "agent", "locked", "missing-drive"]
)
async def test_live_failures_do_not_save_mount(service, tools_iso, failure):
    desktop = DesktopManager(service)
    document = await create(desktop)
    vm_id = document["vm_id"]
    await desktop.start(vm_id)
    instance = service.manager.find(vm_id)
    revision = desktop.get(vm_id).revision
    request = {"revision": revision, "instance_id": instance.view.instance_id}
    expected = None
    if failure == "stale-instance":
        request["instance_id"] = "old-instance"
        expected = ErrorCode.STALE_FRAME
    elif failure == "stale-revision":
        request["revision"] = revision + 1
        expected = ErrorCode.STATE_CONFLICT
    elif failure == "agent":
        await service.driver.acquire(vm_id, instance_id=instance.view.instance_id, run_id="test")
        expected = ErrorCode.BUSY
    elif failure == "locked":
        write_behavior(instance, {"tools_media_fail": True})
    else:
        write_behavior(instance, {"tools_drive_missing": True})
        expected = ErrorCode.STATE_CONFLICT
    with pytest.raises(PaletteError) as error:
        await change_tools_media(desktop, vm_id, request, mount=True)
    if expected:
        assert error.value.code == expected
    assert desktop.get(vm_id).tools_iso_sha256 is None
    assert desktop.get(vm_id).revision == revision


@pytest.mark.parametrize("damage", ["iso", "manifest", "missing", "architecture"])
async def test_media_is_checked_before_mutation(service, tools_iso, damage):
    profile = service.config.profile("default")
    if damage == "iso":
        tools_iso.write_bytes(b"corrupt".ljust(32768, b"\0"))
    elif damage == "manifest":
        tools_iso.with_suffix(".json").write_text('{"sha256":"bad"}', encoding="utf-8")
    elif damage == "missing":
        tools_iso.unlink()
    else:
        profile = profile.model_copy(
            update={"qemu": profile.qemu.model_copy(update={"arch": "aarch64"})}
        )
    with pytest.raises(PaletteError) as error:
        await checked_tools_iso(service.config, profile)
    assert error.value.code == (
        ErrorCode.UNSUPPORTED_CAPABILITY if damage == "architecture" else ErrorCode.IMAGE_INVALID
    )


async def test_changed_iso_is_not_substituted_on_restart(service, tools_iso):
    desktop = DesktopManager(service)
    vm_id = (await create(desktop))["vm_id"]
    await change_tools_media(desktop, vm_id, {"revision": 0}, mount=True)
    old = desktop.get(vm_id).tools_iso_sha256
    data = json.loads(tools_iso.with_suffix(".json").read_text(encoding="utf-8"))
    tools_iso.write_bytes(b"new valid version".ljust(32768, b"\0"))
    data["sha256"] = hashlib.sha256(tools_iso.read_bytes()).hexdigest()
    tools_iso.with_suffix(".json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(PaletteError, match="重新装载"):
        await desktop.start(vm_id)
    assert desktop.get(vm_id).tools_iso_sha256 == old


async def test_linux_preset_rejects_windows_media(service, tools_iso):
    desktop = DesktopManager(service)
    vm_id = (await create(desktop, family="linux"))["vm_id"]
    with pytest.raises(PaletteError) as error:
        await change_tools_media(desktop, vm_id, {"revision": 0}, mount=True)
    assert error.value.code == ErrorCode.UNSUPPORTED_CAPABILITY


async def test_guest_refused_eject_is_not_reported_as_ejected(service, tools_iso):
    desktop = DesktopManager(service)
    vm_id = (await create(desktop))["vm_id"]
    await desktop.start(vm_id)
    instance = service.manager.find(vm_id)
    request = {"revision": desktop.get(vm_id).revision, "instance_id": instance.view.instance_id}
    mounted = await change_tools_media(desktop, vm_id, request, mount=True)
    write_behavior(instance, {"tools_eject_ignored": True})
    with pytest.raises(PaletteError) as error:
        await change_tools_media(
            desktop, vm_id, {**request, "revision": mounted["revision"]}, mount=False
        )
    assert error.value.code == ErrorCode.ACTION_OUTCOME_UNKNOWN
    assert desktop.get(vm_id).tools_iso_sha256 is not None
