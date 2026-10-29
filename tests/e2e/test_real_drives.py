"""Opt-in real QEMU acceptance for multiple drives and dedicated tools hotplug.

Set PALETTE_DRIVES_QEMU to an x86_64 QEMU executable. This checks actual QEMU
device/block state with disposable media; it does not claim Windows installation acceptance.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from palette.audit import NullAuditLog
from palette.config import HostConfig, PaletteConfig, Profile
from palette.desktop import DesktopManager
from palette.managed_disk_delete import delete_managed_disk
from palette.managed_drives import managed_drive_path
from palette.service import PaletteService
from palette.tools_media import change_tools_media
from palette.vm.launcher import run_tool
from palette.vm_settings import settings_from_profile

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not os.environ.get("PALETTE_DRIVES_QEMU"), reason="set PALETTE_DRIVES_QEMU"),
]


async def test_real_multiple_optical_drives_and_tools_hotplug_preserve_system_media(tmp_path):
    qemu = Path(os.environ["PALETTE_DRIVES_QEMU"]).resolve()
    base = tmp_path / "boot.raw"
    base.write_bytes((b"\xfa\xf4\xeb\xfd".ljust(510, b"\0") + b"\x55\xaa").ljust(1024**2, b"\0"))
    data = tmp_path / "data.raw"
    data.write_bytes(bytes(1024**2))
    data_overlay = tmp_path / "data.qcow2"
    code, _, _ = await run_tool(
        [
            str(qemu.with_name("qemu-img" + qemu.suffix)),
            "create",
            "-f",
            "qcow2",
            "-F",
            "raw",
            "-b",
            str(base),
            str(data_overlay),
        ],
        timeout=30,
    )
    assert code == 0
    installer = tmp_path / "安装介质.iso"
    installer.write_bytes(bytes(32768))
    directory = tmp_path / "images" / "guest-tools"
    directory.mkdir(parents=True)
    tools = directory / "palette-tools-windows-x86_64.iso"
    tools.write_bytes(bytes(65536))
    tools.with_suffix(".json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "guest_os": "windows",
                "arch": "x86_64",
                "version": "test",
                "size_bytes": tools.stat().st_size,
                "sha256": hashlib.sha256(tools.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    profile = Profile.model_validate(
        {
            "profile_id": "real-drives",
            "qemu": {
                "binary": str(qemu),
                "qemu_img": str(qemu.with_name("qemu-img" + qemu.suffix)),
                "accelerator": "tcg",
                "cpu": "max",
                "machine": "q35,smm=off",
            },
            "images": {"base": str(base), "format": "raw", "size_gib": 1},
            "resources": {"vcpus": 1, "memory_mib": 256, "process_memory_limit_mib": None},
            "network": {"mode": "offline"},
        }
    )
    service = PaletteService(
        PaletteConfig(
            profiles={profile.profile_id: profile},
            config_root=tmp_path,
            host=HostConfig(
                default_profile=profile.profile_id, runtime_root=str(tmp_path / "runtime")
            ),
        ),
        audit=NullAuditLog(),
    )
    await service.startup()
    desktop = DesktopManager(service)
    vm_id = None
    try:
        settings = settings_from_profile(profile, str(installer)).model_dump()
        settings["boot_order"] = "disk"
        settings["drives"] = [
            *settings["drives"],
            {"id": "drive-blank", "media": "disk", "size_gib": 1, "interface": "usb"},
            {
                "id": "drive-scsi",
                "media": "disk",
                "size_gib": 1,
                "format": "raw",
                "interface": "scsi",
            },
            {
                "id": "drive-nvme",
                "media": "disk",
                "path": str(data_overlay),
                "format": "qcow2",
                "interface": "nvme",
                "import_copy": True,
                "read_only": False,
            },
            {"id": "drive-data", "media": "disk", "path": str(data), "read_only": False},
            {
                "id": "drive-overlay",
                "interface": "ide",
                "media": "disk",
                "path": str(data_overlay),
                "format": "qcow2",
                "read_only": False,
            },
            {"id": "drive-disc", "media": "cdrom", "path": str(installer)},
            {"id": "drive-empty", "media": "cdrom"},
        ]
        vm_id = (
            await desktop.create(
                {
                    "name": "Real drives",
                    "os_preset": "windows",
                    "profile_id": profile.profile_id,
                    "source": "image",
                    "path": str(base),
                    "image_format": "raw",
                    "vcpus": 1,
                    "memory_mib": 256,
                    "disk_gib": 1,
                    "network": "offline",
                    "settings": settings,
                }
            )
        )["vm_id"]
        definition = desktop.get(vm_id)
        managed = next(
            drive for drive in desktop.settings(definition).drives if drive.id == "drive-blank"
        )
        blank_path = managed_drive_path(desktop, definition, managed)
        info = await service.manager.images_for(profile).inspect_image(blank_path)
        assert info.format == "qcow2" and info.virtual_size == 1024**3
        assert info.chain == (blank_path,)
        await desktop.start(vm_id)
        instance = service.manager.find(vm_id)
        original = await instance.qmp.execute("query-block")
        (tmp_path / "blocks.json").write_text(json.dumps(original, indent=2), encoding="utf-8")
        assert any(
            item.get("qdev") == "drive-empty" and not item.get("inserted") for item in original
        )
        assert any(
            item.get("qdev") == "/machine/peripheral/drive-data/virtio-backend"
            and not item["inserted"]["ro"]
            for item in original
        )
        assert any(
            item.get("qdev") == "drive-blank" and not item["inserted"]["ro"] for item in original
        )
        assert sum(item.get("inserted", {}).get("ro", False) for item in original) >= 2
        devices = await instance.qmp.execute("qom-list", {"path": "/machine/peripheral"})
        for model in ("nvme", "ide-hd", "scsi-hd", "usb-bot"):
            assert any(item["type"] == f"child<{model}>" for item in devices)
        copied = desktop.get(vm_id).data_disks["drive-nvme"]
        from palette.managed_drives import managed_disk_path

        imported = managed_disk_path(desktop, vm_id, copied)
        assert imported != data_overlay and copied.virtual_size == 1024**2
        request = {
            "revision": desktop.get(vm_id).revision,
            "instance_id": instance.view.instance_id,
        }
        first = await change_tools_media(desktop, vm_id, request, mount=True)
        blocks = await instance.qmp.execute("query-block")
        assert [
            item for item in blocks if "palette-tools-cd" not in item.get("qdev", "")
        ] == original
        cd = next(item for item in blocks if item.get("qdev") == "palette-tools-cd")
        assert cd["inserted"]["ro"] and Path(cd["inserted"]["file"]).resolve() == tools.resolve()
        devices = await instance.qmp.execute("qom-list", {"path": "/machine/peripheral"})
        assert any(
            item["name"] == "palette-tools-cd" and item["type"] == "child<scsi-cd>"
            for item in devices
        )
        second = await change_tools_media(
            desktop, vm_id, {**request, "revision": first["revision"]}, mount=True
        )
        await change_tools_media(
            desktop, vm_id, {**request, "revision": second["revision"]}, mount=False
        )
        blocks = await instance.qmp.execute("query-block")
        assert [
            item for item in blocks if "palette-tools-cd" not in item.get("qdev", "")
        ] == original
        assert not next(item for item in blocks if item.get("qdev") == cd["qdev"]).get("inserted")
        await desktop.stop(vm_id, force=True)
        qemu_io = str(qemu.with_name("qemu-io" + qemu.suffix))
        code, _, err = await run_tool(
            [qemu_io, "-f", "qcow2", "-c", "write -P 0x5a 0 4096", str(blank_path)],
            timeout=30,
        )
        assert code == 0, err
        await desktop.start(vm_id)
        restarted = service.manager.find(vm_id)
        blocks = await restarted.qmp.execute("query-block")
        assert len(blocks) == len(original)
        await desktop.stop(vm_id, force=True)
        await desktop.reset(vm_id)
        await desktop.stop(vm_id, force=True)
        assert managed_drive_path(desktop, desktop.get(vm_id), managed) == blank_path
        code, _, err = await run_tool(
            [qemu_io, "-f", "qcow2", "-c", "read -P 0x5a 0 4096", str(blank_path)],
            timeout=30,
        )
        assert code == 0, err
        await delete_managed_disk(
            desktop,
            vm_id,
            {
                "revision": desktop.get(vm_id).revision,
                "drive_id": managed.id,
            },
        )
        assert not blank_path.exists() and not blank_path.parents[1].exists()
        assert managed.id not in desktop.get(vm_id).data_disks
        await desktop.start(vm_id)
        instance = service.manager.find(vm_id)
        blocks = await instance.qmp.execute("query-block")
        assert not any("drive-blank" in item.get("qdev", "") for item in blocks)
        initial_path = instance.paths.overlay
        await desktop.stop(vm_id, force=True)
        current = await desktop.get_settings(vm_id)
        await desktop.resize_disk(
            vm_id,
            {
                "revision": current["revision"],
                "drive_id": "drive-scsi",
                "disk_gib": 2,
            },
        )
        current = await desktop.get_settings(vm_id)
        await desktop.update(
            vm_id,
            {
                "revision": current["revision"],
                "name": current["draft"]["name"],
                "vcpus": 1,
                "memory_mib": 256,
                "network": "offline",
                "iso_attached": False,
                "settings": {
                    **current["settings"],
                    "drives": [
                        drive
                        for drive in current["settings"]["drives"]
                        if drive["id"] != "drive-primary"
                    ],
                },
            },
        )
        assert not initial_path.exists()
        await desktop.start(vm_id)
        instance = service.manager.find(vm_id)
        blocks = await instance.qmp.execute("query-block")
        assert not any("drive-primary" in item.get("qdev", "") for item in blocks)
        assert any("drive-data" in item.get("qdev", "") for item in blocks)
    finally:
        if vm_id and service.manager.find(vm_id):
            await desktop.stop(vm_id, force=True)
        await service.shutdown()
