"""Opt-in real QEMU CPU display acceptance; firmware is not desktop latency evidence."""

from __future__ import annotations

import asyncio
import io
import os
from pathlib import Path

import pytest
from PIL import Image
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from palette.config import HostConfig, PaletteConfig, Profile
from palette.display import DisplayGateway
from palette.service import PaletteService

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not (os.environ.get("PALETTE_DBUS_QEMU") and os.environ.get("PALETTE_DBUS_HELPER")),
        reason="set PALETTE_DBUS_QEMU and PALETTE_DBUS_HELPER for real display acceptance",
    ),
]


async def test_real_pixels_helper_crash_reconnect_and_input_release(tmp_path, record_property):
    qemu = Path(os.environ["PALETTE_DBUS_QEMU"]).resolve()
    helper = Path(os.environ["PALETTE_DBUS_HELPER"]).resolve()
    base = tmp_path / "halt.raw"
    original = (b"\xfa\xf4\xeb\xfd".ljust(510, b"\0") + b"\x55\xaa").ljust(1024**2, b"\0")
    base.write_bytes(original)
    accelerator = os.environ.get("PALETTE_DBUS_ACCEL", "tcg")
    profile = Profile.model_validate(
        {
            "profile_id": "display",
            "qemu": {
                "binary": str(qemu),
                "qemu_img": str(qemu.with_name("qemu-img" + qemu.suffix)),
                "accelerator": accelerator,
                "cpu": "max" if accelerator == "tcg" else None,
                "machine": "q35",
            },
            "images": {"base": str(base), "format": "raw", "size_gib": 1},
            "resources": {"vcpus": 1, "memory_mib": 256, "process_memory_limit_mib": None},
            "network": {"mode": "offline"},
            "guest_tools": {"enabled": False},
        }
    )
    service = PaletteService(
        PaletteConfig(
            profiles={"display": profile},
            config_root=tmp_path,
            host=HostConfig(
                default_profile="display",
                runtime_root=str(tmp_path / "runtime"),
                experimental_display="dbus",
                experimental_display_helper=str(helper),
            ),
        )
    )
    service.manager.console_enabled = True
    gateway = DisplayGateway(service)
    await service.startup()
    await gateway.start()
    try:
        instance = await service.manager.start("display")
        bridge = instance.display_bridge
        assert bridge is not None
        async with asyncio.timeout(10):
            while bridge.source.frame is None:
                assert not bridge.source.closed, list(bridge.stderr)
                await asyncio.sleep(0.02)
        assert instance.capture is not None
        async with asyncio.timeout(10):
            while True:
                captured = await instance.capture.capture(
                    display_id="video0", head=0, sink=tmp_path / "compare.png"
                )
                frame = bridge.source.frame
                with Image.open(io.BytesIO(captured.png_bytes)) as image:
                    # Firmware can resize during startup. Require an exact, complete
                    # match after resynchronization, never pad unknown pixels.
                    if (
                        frame
                        and image.size == (frame.width, frame.height)
                        and image.convert("RGBA").tobytes() == frame.rgba
                    ):
                        break
                assert not bridge.source.closed, list(bridge.stderr)
                await asyncio.sleep(0.05)
        await service.observe("display", actor="operator")
        ticket = gateway.ticket("display", instance.view.instance_id)
        assert ticket["provider"] == "raw", list(bridge.stderr)
        lease = await service.control.acquire("display", instance.view.instance_id)
        await service.control.input(
            "display",
            lease["lease_id"],
            1,
            instance.view.display_epoch,
            frame.width,
            frame.height,
            [{"type": "key", "key": "shift", "down": True}],
        )
        async with connect(ticket["url"], origin="app://palette", max_size=None) as ws:
            await ws.recv()
            assert bridge.process is not None
            bridge.process.kill()
            with pytest.raises(ConnectionClosed):
                while True:
                    await ws.recv()
        async with asyncio.timeout(5):
            while "display" in service.control.leases:  # noqa: ASYNC110 - bounded release observation
                await asyncio.sleep(0.01)
        assert not instance.controller.ledger_snapshot()["keys"]
        generation = bridge.source.generation
        await service.manager.start_display_bridge(instance)
        replacement = instance.display_bridge
        assert replacement and replacement.source.generation != generation
        async with asyncio.timeout(5):
            while replacement.source.frame is None:
                assert not replacement.source.closed, list(replacement.stderr)
                await asyncio.sleep(0.02)
        record_property(
            "execution",
            {
                "accelerator": accelerator,
                "qemu": str(qemu),
                "host": os.name,
                "guest": "x86_64 firmware",
                "pixel_format": "RGBA",
                "helper_restart": True,
            },
        )
    finally:
        await gateway.close()
        await service.manager.stop("display", force=True)
        await service.shutdown()
    assert base.read_bytes() == original
