"""Opt-in real QEMU acceptance using a disposable keyboard-driven VGA boot sector.

Set PALETTE_VIRTIO_VGA_QEMU to a QEMU x86_64 executable. This verifies the
actual framebuffer, input route and VNC handshake, without claiming OS/driver acceptance.
"""

from __future__ import annotations

import asyncio
import os
import struct
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from palette.audit import NullAuditLog
from palette.config import HostConfig, PaletteConfig, Profile
from palette.display import VERSION, vnc_response
from palette.service import PaletteService

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not os.environ.get("PALETTE_VIRTIO_VGA_QEMU"),
        reason="set PALETTE_VIRTIO_VGA_QEMU for real VirtIO VGA acceptance",
    ),
]


@pytest.mark.parametrize("enhancements", [False, True])
async def test_real_virtio_vga_framebuffer_keyboard_and_vnc(tmp_path, enhancements, monkeypatch):
    qemu = Path(os.environ["PALETTE_VIRTIO_VGA_QEMU"]).resolve()
    image = tmp_path / "keyboard.raw"
    # Real-mode BIOS guest: set mode 13h, fill blue, wait for a key, then change color.
    program = bytes.fromhex(
        "fa31c08ed88ed0bc007cfb"  # CLI; DS=SS=0; SP=7c00h; STI
        "b81300cd10b800a08ec0fcbb0100"  # mode 13h; ES=a000h; CLD; BX=blue
        "31ffb900fa88d8f3aa"  # DI=0; CX=64000; AL=BL; REP STOSB
        "30e4cd16bb0200ebee"  # BIOS wait for key; BX=green; jump to fill
    )
    base = (program.ljust(510, b"\0") + b"\x55\xaa").ljust(1024 * 1024, b"\0")
    image.write_bytes(base)
    profile = Profile.model_validate(
        {
            "profile_id": "vga-acceptance",
            "qemu": {
                "binary": str(qemu),
                "qemu_img": str(qemu.with_name("qemu-img" + qemu.suffix)),
                "accelerator": "tcg",
                "cpu": "max",
                # This minimal BIOS guest uses USB polling without SMM services.
                "machine": "q35,smm=off",
            },
            "images": {"base": str(image), "format": "raw", "size_gib": 1},
            "devices": {"display_model": "virtio-vga", "default_resolution": True},
            "resources": {"vcpus": 1, "memory_mib": 256, "process_memory_limit_mib": None},
            "timeouts": {
                "settle_after_input_ms": 200,
                "qmp_handshake_seconds": 2.0 if enhancements else 10.0,
            },
            "guest_agent": {"enabled": enhancements},
            "guest_tools": {"enabled": enhancements},
            "guest_desktop": {
                "clipboard": enhancements,
                "resize": enhancements,
                "shared_folders": enhancements,
            },
            # BIOS USB polling is much slower than a desktop guest's HID driver.
            "text": {"char_interval_ms": 200},
            "network": {"mode": "offline"},
        }
    )
    config = PaletteConfig(
        profiles={profile.profile_id: profile},
        config_root=tmp_path,
        host=HostConfig(default_profile=profile.profile_id, runtime_root=str(tmp_path / "runtime")),
    )
    service = PaletteService(config, audit=NullAuditLog())
    service.manager.console_enabled = True
    if enhancements and os.name == "nt":
        from palette import windows_unix

        original_connect = windows_unix.open_connection

        async def connect_after_creation(endpoint, **kwargs):
            if Path(endpoint).stem == "tools":
                await asyncio.sleep(0.25)
            return await original_connect(endpoint, **kwargs)

        monkeypatch.setattr(windows_unix, "open_connection", connect_after_creation)
    await service.startup()
    try:
        instance = await service.manager.start("vga-test")
        async with asyncio.timeout(25):
            while True:
                observed, png = await service.observe_image("vga-test")
                if observed.ok:
                    screen = Image.open(BytesIO(png)).convert("RGB")
                    if (
                        screen.size in {(320, 200), (640, 400)}
                        and screen.getpixel((100, 100))[:2] == (0, 0)
                        and screen.getpixel((100, 100))[2] >= 160
                    ):
                        break
                await asyncio.sleep(0.1)
        assert instance.plan and "virtio-vga,id=video0,xres=1280,yres=800" in instance.plan.argv
        assert any("usb-kbd," in arg and "display=video0" in arg for arg in instance.plan.argv)
        assert any("usb-tablet," in arg and "display=video0" in arg for arg in instance.plan.argv)
        assert instance.console_address and instance.console_password
        reader, writer = await asyncio.open_connection(*instance.console_address)
        try:
            async with asyncio.timeout(5):
                assert await reader.readexactly(12) == VERSION
                writer.write(VERSION)
                count = (await reader.readexactly(1))[0]
                assert 2 in await reader.readexactly(count)
                writer.write(b"\x02")
                writer.write(vnc_response(instance.console_password, await reader.readexactly(16)))
                await writer.drain()
                assert await reader.readexactly(4) == bytes(4)
                writer.write(b"\x01")
                await writer.drain()
                header = await reader.readexactly(24)
                assert struct.unpack_from(">HH", header) == screen.size
        finally:
            writer.close()
            await writer.wait_closed()
        assert observed.frame
        result, _ = await service.act(
            "vga-test",
            instance_id=instance.view.instance_id,
            frame_id=observed.frame.frame_id,
            request_id="change-color",
            action={"type": "type_text", "text": "a", "mode": "keyboard"},
        )
        assert result.ok and result.effect_state == "submitted", result.error
        async with asyncio.timeout(10):
            while True:
                result, png = await service.observe_image("vga-test")
                screen = Image.open(BytesIO(png)).convert("RGB")
                if (
                    result.ok
                    and screen.getpixel((100, 100))[::2] == (0, 0)
                    and screen.getpixel((100, 100))[1] >= 160
                ):
                    break
                await asyncio.sleep(0.1)
    finally:
        if "png" in locals():
            (tmp_path / "last-screen.png").write_bytes(png)
        # This tiny BIOS guest has no ACPI shutdown handler. Stop only this disposable VM.
        if service.manager.find("vga-test") is not None:
            await service.manager.stop("vga-test", force=True)
        await service.shutdown()
    assert image.read_bytes() == base
