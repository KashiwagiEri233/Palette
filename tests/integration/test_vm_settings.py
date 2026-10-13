"""Hardware edits and disk transactions retain operator-owned state."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from palette.desktop import DesktopManager
from palette.errors import ErrorCode, PaletteError
from palette.service import PaletteService
from palette.vm.launcher import run_tool


async def machine(service: PaletteService) -> tuple[DesktopManager, str]:
    desktop = DesktopManager(service)
    profile = service.config.profile("default")
    definition = await desktop.create(
        {
            "name": "Settings test",
            "profile_id": "default",
            "source": "image",
            "path": profile.images.base,
            "vcpus": 1,
            "memory_mib": 512,
            "disk_gib": 24,
            "network": "offline",
        }
    )
    return desktop, definition["vm_id"]


def update_payload(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "revision": data["revision"],
        "name": data["draft"]["name"],
        "vcpus": data["draft"]["vcpus"],
        "memory_mib": data["draft"]["memory_mib"],
        "network": data["draft"]["network"],
        "iso_attached": data["iso_attached"],
        "settings": data["settings"],
    }


async def test_settings_save_revision_and_launch(service: PaletteService, tmp_path: Path) -> None:
    desktop, vm_id = await machine(service)
    data = await desktop.get_settings(vm_id)
    payload = update_payload(data)
    iso = tmp_path / "second.iso"
    iso.write_bytes(b"installer")
    payload.update(vcpus=2, memory_mib=1024, network="nat")
    payload["settings"].update(iso_path=str(iso), boot_order="cdrom", audio_enabled=True)
    payload["settings"]["display"].update(default_resolution=True, width=1920, height=1080)
    payload["settings"]["network"].update(
        model="virtio-net-pci",
        mac="52:54:00:11:22:33",
        forwards=[{"protocol": "tcp", "host_port": 8081, "guest_port": 80}],
    )
    saved = await desktop.update(vm_id, payload)
    assert saved["schema_version"] == 2
    assert saved["revision"] == data["revision"] + 1
    with pytest.raises(PaletteError) as stale:
        await desktop.update(vm_id, payload)
    assert stale.value.code == ErrorCode.STATE_CONFLICT
    restored = DesktopManager(service)
    assert (await restored.get_settings(vm_id))["settings"] == payload["settings"]
    await restored.start(vm_id)
    instance = service.manager.get(vm_id)
    assert instance.plan is not None
    argv = instance.plan.argv
    assert argv[argv.index("-m") + 1] == "1024"
    assert argv[argv.index("-smp") + 1] == "2"
    assert any("xres=1920,yres=1080" in value for value in argv)
    assert any("hostfwd=tcp:127.0.0.1:8081-:80" in value for value in argv)
    assert any("hda-output" in value for value in argv)
    assert not any("hda-duplex" in value for value in argv)
    assert any("scsi-cd" in value and "bootindex=0" in value for value in argv)
    readonly = await restored.get_settings(vm_id)
    assert not readonly["editable"]
    assert readonly["disk"]["virtual_bytes"] == 24 * 1024**3
    with pytest.raises(PaletteError, match="shut down"):
        await restored.update(vm_id, update_payload(readonly))
    with pytest.raises(PaletteError, match="shut down"):
        await restored.resize_disk(vm_id, {"revision": readonly["revision"], "disk_gib": 32})


async def test_legacy_snapshot_migrates_only_on_save(service: PaletteService) -> None:
    desktop, vm_id = await machine(service)
    definition = desktop.get(vm_id)
    document = definition.model_dump(mode="json")
    document["schema_version"] = 1
    document.pop("revision")
    document["draft"].pop("settings")
    document["draft"].pop("iso_attached")
    document["profile"]["devices"]["display_model"] = "ramfb"
    document["profile"]["resources"]["process_memory_limit_mib"] = 8192
    document["profile"]["network"].update(
        extra_options="hostname=legacy", hostfwd=["tcp:127.0.0.1:8089-:89"]
    )
    path = desktop.directory / (vm_id + ".json")
    path.write_text(json.dumps(document))
    original = path.read_bytes()
    restored = DesktopManager(service)
    data = await restored.get_settings(vm_id)
    assert data["settings"]["display"]["model"] == "ramfb"
    assert path.read_bytes() == original
    payload = update_payload(data)
    payload["memory_mib"] = 12288
    await restored.update(vm_id, payload)
    updated = restored.get(vm_id)
    assert updated.schema_version == 2
    assert updated.profile.resources.process_memory_limit_mib >= 14336
    assert updated.profile.network.extra_options == "hostname=legacy"
    assert updated.profile.network.hostfwd == ["tcp:127.0.0.1:8089-:89"]


async def test_resize_preserves_chain_bytes_vars_and_reset_capacity(
    service: PaletteService,
) -> None:
    desktop, vm_id = await machine(service)
    await desktop.start(vm_id)
    instance = service.manager.get(vm_id)
    disk = instance.paths.overlay
    marker = disk.with_suffix(".userdata")
    marker.write_bytes(b"guest data")
    await desktop.stop(vm_id)
    data = await desktop.get_settings(vm_id)
    chain = (await service.manager._images(desktop.get(vm_id).profile).inspect_image(disk)).chain
    await desktop.resize_disk(vm_id, {"revision": data["revision"], "disk_gib": 32})
    expanded = await desktop.get_settings(vm_id)
    assert expanded["disk"]["virtual_bytes"] == 32 * 1024**3
    assert not desktop._operation_path(vm_id).exists()
    assert marker.read_bytes() == b"guest data"
    assert (
        await service.manager._images(desktop.get(vm_id).profile).inspect_image(disk)
    ).chain == chain
    with pytest.raises(PaletteError):
        await desktop.resize_disk(vm_id, {"revision": expanded["revision"], "disk_gib": 24})
    await desktop.start(vm_id)
    assert service.manager.get(vm_id).paths.overlay == disk
    await desktop.reset(vm_id)
    replacement = service.manager.get(vm_id).paths.overlay
    assert replacement != disk
    assert (
        await service.manager._images(desktop.get(vm_id).profile).inspect_image(replacement)
    ).virtual_size == 32 * 1024**3


async def test_resize_before_first_boot(service: PaletteService) -> None:
    desktop, vm_id = await machine(service)
    await desktop.resize_disk(vm_id, {"revision": 0, "disk_gib": 40})
    assert desktop.get(vm_id).storage_owner is None
    await desktop.start(vm_id)
    disk = service.manager.get(vm_id).paths.overlay
    assert (
        await service.manager._images(desktop.get(vm_id).profile).inspect_image(disk)
    ).virtual_size == 40 * 1024**3


async def test_resize_journal_recovers_after_definition_write_failure(
    service: PaletteService, monkeypatch: pytest.MonkeyPatch
) -> None:
    desktop, vm_id = await machine(service)
    await desktop.start(vm_id)
    await desktop.stop(vm_id)
    original_save = desktop._save

    def fail_save(_: Any) -> None:
        raise OSError("injected save failure")

    monkeypatch.setattr(desktop, "_save", fail_save)
    with pytest.raises(OSError, match="injected"):
        await desktop.resize_disk(vm_id, {"revision": 0, "disk_gib": 32})
    assert desktop._operation_path(vm_id).exists()
    monkeypatch.setattr(desktop, "_save", original_save)
    restored = DesktopManager(service)
    await restored.recover_operations()
    assert restored.get(vm_id).draft.disk_gib == 32
    assert not restored._operation_path(vm_id).exists()


async def test_failed_resize_recovers_original_capacity(
    service: PaletteService, monkeypatch: pytest.MonkeyPatch
) -> None:
    desktop, vm_id = await machine(service)
    await desktop.start(vm_id)
    await desktop.stop(vm_id)

    async def fail_resize(argv: list[str], **kwargs: Any) -> tuple[int, str, str]:
        if "resize" in argv:
            return 1, "", "injected resize failure"
        return await run_tool(argv, **kwargs)

    monkeypatch.setattr("palette.desktop.run_tool", fail_resize)
    with pytest.raises(PaletteError):
        await desktop.resize_disk(vm_id, {"revision": 0, "disk_gib": 32})
    assert desktop.get(vm_id).draft.disk_gib == 24
    assert not desktop._operation_path(vm_id).exists()


async def test_edit_and_start_share_lock(
    service: PaletteService, monkeypatch: pytest.MonkeyPatch
) -> None:
    desktop, vm_id = await machine(service)
    payload = update_payload(await desktop.get_settings(vm_id))
    entered, release = asyncio.Event(), asyncio.Event()
    original = desktop.probe.get

    async def paused(profile: Any) -> dict[str, Any]:
        entered.set()
        await release.wait()
        return await original(profile)

    monkeypatch.setattr(desktop.probe, "get", paused)
    edit = asyncio.create_task(desktop.update(vm_id, payload))
    await entered.wait()
    start = asyncio.create_task(desktop.start(vm_id))
    await asyncio.sleep(0)
    assert service.manager.find(vm_id) is None
    release.set()
    await edit
    await start
    assert service.manager.get(vm_id).profile.resources.memory_mib == payload["memory_mib"]


async def test_uncertain_disk_integrity_blocks_start_and_keeps_journal(
    service: PaletteService, monkeypatch: pytest.MonkeyPatch
) -> None:
    desktop, vm_id = await machine(service)
    await desktop.start(vm_id)
    await desktop.stop(vm_id)
    storage = desktop._storage(desktop.get(vm_id))
    assert storage is not None
    info = await service.manager._images(desktop.get(vm_id).profile).inspect_image(storage.overlay)
    path = desktop._operation_path(vm_id)
    desktop._write_operation(
        path,
        {
            "overlay": str(storage.overlay),
            "chain": [str(entry) for entry in info.chain],
            "before_bytes": info.virtual_size,
            "target_bytes": 32 * 1024**3,
            "disk_gib": 32,
        },
    )

    async def corrupt(argv: list[str], **kwargs: Any) -> tuple[int, str, str]:
        if "check" in argv:
            return 2, "", "injected corruption"
        return await run_tool(argv, **kwargs)

    monkeypatch.setattr("palette.desktop.run_tool", corrupt)
    with pytest.raises(PaletteError):
        await desktop.start(vm_id)
    assert path.exists()
    assert desktop.get(vm_id).draft.disk_gib == 24
    assert storage.overlay.exists()
    assert service.manager.get(vm_id).state == "STOPPED"


async def test_missing_exit_evidence_prevents_edits(service: PaletteService) -> None:
    desktop, vm_id = await machine(service)
    await desktop.start(vm_id)
    await desktop.stop(vm_id)
    data = await desktop.get_settings(vm_id)
    storage = desktop._storage(desktop.get(vm_id))
    assert storage is not None
    service.manager._images(desktop.get(vm_id).profile).update_manifest(
        storage, {"process_exited": False}
    )
    with pytest.raises(PaletteError, match="previous process"):
        await desktop.update(vm_id, update_payload(data))
    assert desktop.get(vm_id).revision == 0


async def test_trusted_cpu_override_conflict_is_rejected(service: PaletteService) -> None:
    desktop, vm_id = await machine(service)
    definition = desktop.get(vm_id)
    desktop._save(
        definition.model_copy(
            update={
                "profile": definition.profile.model_copy(
                    update={
                        "qemu": definition.profile.qemu.model_copy(
                            update={"extra_args": ["-smp", "4"]}
                        )
                    }
                )
            }
        )
    )
    payload = update_payload(await desktop.get_settings(vm_id))
    payload["vcpus"] = 2
    with pytest.raises(PaletteError):
        await desktop.update(vm_id, payload)
    assert desktop.get(vm_id).revision == 0


@pytest.mark.parametrize(
    "settings_patch",
    [
        {"network": {"mac": "ff:ff:ff:ff:ff:ff"}},
        {"network": {"forwards": [{"protocol": "tcp", "host_port": 0, "guest_port": 80}]}},
        {"display": {"width": 8192, "height": 8192}},
        {"audio_enabled": "true"},
        {"boot_order": "cdrom"},
    ],
)
async def test_invalid_hardware_never_saves(
    service: PaletteService, settings_patch: dict[str, Any]
) -> None:
    desktop, vm_id = await machine(service)
    payload = update_payload(await desktop.get_settings(vm_id))
    for key, value in settings_patch.items():
        if isinstance(value, dict):
            payload["settings"][key].update(value)
        else:
            payload["settings"][key] = value
    with pytest.raises(PaletteError):
        await desktop.update(vm_id, payload)
    assert desktop.get(vm_id).revision == 0
