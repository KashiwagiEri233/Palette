"""Drive interfaces are filtered by actual devices, architecture and machine topology."""

from __future__ import annotations

import pytest

from palette import vm_settings
from palette.vm_settings import CapabilityProbe, DriveSettings


def test_import_defaults_to_an_owned_writable_qcow2_copy():
    drive = DriveSettings.model_validate(
        {"id": "drive-copy", "media": "disk", "path": "/source.raw", "import_copy": True}
    )
    assert drive.format == "qcow2" and not drive.read_only


@pytest.mark.parametrize(
    "arch,machine,ide",
    [("x86_64", "q35", True), ("x86_64", "pc", True), ("aarch64", "virt", False)],
)
async def test_interface_candidates_follow_architecture_and_machine(
    config, profile, monkeypatch, arch, machine, ide
):
    names = {
        "VGA",
        "virtio-blk-pci",
        "ide-hd",
        "ide-cd",
        "virtio-scsi-pci",
        "scsi-hd",
        "scsi-cd",
        "usb-bot",
        "nvme",
    }

    async def run(command, **kwargs):
        if command[-2:] == ["-device", "help"]:
            return 0, "\n".join(f'name "{name}"' for name in names), ""
        return 0, "xres=1280 yres=800", ""

    monkeypatch.setattr(vm_settings, "run_tool", run)
    target = profile.model_copy(
        update={"qemu": profile.qemu.model_copy(update={"arch": arch, "machine": machine})}
    )
    caps = await CapabilityProbe(config.host)._probe(target, "qemu")
    assert caps["drive_interfaces"]["disk"] == (
        ["virtio", "ide", "scsi", "nvme", "usb"] if ide else ["virtio", "scsi", "nvme", "usb"]
    )
    assert caps["drive_interfaces"]["cdrom"] == (["ide", "scsi", "usb"] if ide else ["scsi", "usb"])
    names.remove("virtio-scsi-pci")
    names.remove("nvme")
    missing = await CapabilityProbe(config.host)._probe(target, "qemu")
    assert "scsi" not in missing["drive_interfaces"]["disk"]
    assert "nvme" not in missing["drive_interfaces"]["disk"]
    assert "usb" in missing["drive_interfaces"]["cdrom"]
