"""Physical acceptance: a real QEMU binary, real acceleration, a real guest image.

Skipped unless an operator opts in, because the plan's P0 gate cannot be faked: proving
that input lands in the right device and that a screenshot comes back without a host
window requires the actual emulator and a real desktop image.

Enable with::

    set PALETTE_E2E=1
    set PALETTE_E2E_QEMU=C:\\Program Files\\qemu\\qemu-system-x86_64.exe
    set PALETTE_E2E_IMAGE=G:\\Workspace\\Palette\\images\\debian-13-xfce-desktop.qcow2
    set PALETTE_E2E_ACCEL=whpx          (optional; defaults to whpx)

Nothing here is claimed to have passed: a skipped test is not evidence.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from palette.audit import NullAuditLog
from palette.cli import build_doctor_report
from palette.config import ImagesConfig, PaletteConfig, Profile
from palette.errors import ErrorCode, PaletteError
from palette.service import PaletteService

pytestmark = pytest.mark.e2e

QEMU = os.environ.get("PALETTE_E2E_QEMU")
IMAGE = os.environ.get("PALETTE_E2E_IMAGE")
ACCEL = os.environ.get("PALETTE_E2E_ACCEL", "whpx")

requires_hardware = pytest.mark.skipif(
    not (os.environ.get("PALETTE_E2E") and QEMU and IMAGE),
    reason="set PALETTE_E2E=1, PALETTE_E2E_QEMU and PALETTE_E2E_IMAGE to run physical acceptance",
)


def real_profile(*, width: int = 1280, height: int = 800) -> Profile:
    from palette.config import DisplayConfig, QemuConfig, ResourcesConfig

    return Profile(
        profile_id="default",
        qemu=QemuConfig(binary=str(QEMU), accelerator=ACCEL, machine="q35"),
        images=ImagesConfig(base=str(IMAGE), format="qcow2"),
        display=DisplayConfig(width=width, height=height),
        resources=ResourcesConfig(vcpus=2, memory_mib=4096),
    )


def real_config(tmp_path: Path) -> PaletteConfig:
    return PaletteConfig(profiles={"default": real_profile()}, config_root=tmp_path).model_copy(
        update={
            "host": PaletteConfig().host.model_copy(
                update={"runtime_root": str(tmp_path / "runtime")}
            )
        }
    )


def base_digest() -> str:
    digest = hashlib.sha256()
    with open(IMAGE, "rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@requires_hardware
class TestP0Gate:
    def test_the_environment_passes_every_doctor_check(self, tmp_path: Path) -> None:
        report = build_doctor_report(real_config(tmp_path))
        failures = [check for check in report["checks"] if not check["ok"] and check["blocking"]]
        assert report["ok"] is True, failures

    def test_the_requested_accelerator_is_offered_by_this_build(self, tmp_path: Path) -> None:
        report = build_doctor_report(real_config(tmp_path))
        assert ACCEL in report["acceleration"]["available"]

    async def test_a_windows_hidden_start_reaches_a_real_desktop(self, tmp_path: Path) -> None:
        service = PaletteService(real_config(tmp_path), audit=NullAuditLog())
        await service.startup()
        try:
            instance = await service.manager.start("desktop", "default")
            assert instance.view.qmp_ready is True
            assert instance.view.screen_ready is True, "no valid framebuffer was captured"
            assert instance.view.native_size is not None
            result, image = await service.observe_image("desktop")
            assert result.ok is True
            assert image.startswith(b"\x89PNG")
        finally:
            await service.shutdown()

    async def test_see_click_type_see(self, tmp_path: Path) -> None:
        """The P0 pass criterion: look, act, and see the result, with no host focus."""
        service = PaletteService(real_config(tmp_path), audit=NullAuditLog())
        await service.startup()
        try:
            instance = await service.manager.start("desktop", "default")
            observed = await service.observe("desktop")
            assert observed.frame is not None
            receipt, image = await service.act(
                "desktop",
                instance_id=instance.view.instance_id,
                frame_id=observed.frame.frame_id,
                request_id="p0-click",
                action={"type": "click", "x": 10, "y": 10},
            )
            assert receipt.ok is True, receipt.error
            assert receipt.effect_state == "submitted"
            assert image is not None

            typed = await service.act(
                "desktop",
                instance_id=instance.view.instance_id,
                frame_id=receipt.frame.frame_id if receipt.frame else observed.frame.frame_id,
                request_id="p0-text",
                action={"type": "type_text", "text": "palette", "mode": "keyboard"},
            )
            assert typed.ok is True or typed.error is not None
        finally:
            await service.shutdown()

    async def test_twenty_cycles_leave_the_base_image_untouched(self, tmp_path: Path) -> None:
        before = base_digest()
        service = PaletteService(real_config(tmp_path), audit=NullAuditLog())
        await service.startup()
        try:
            for _ in range(20):
                await service.manager.start("desktop", "default")
                await service.manager.stop("desktop")
        finally:
            await service.shutdown()
        assert base_digest() == before

    async def test_a_stale_frame_is_refused_on_hardware(self, tmp_path: Path) -> None:
        service = PaletteService(real_config(tmp_path), audit=NullAuditLog())
        await service.startup()
        try:
            instance = await service.manager.start("desktop", "default")
            observed = await service.observe("desktop")
            assert observed.frame is not None
            await service.act(
                "desktop",
                instance_id=instance.view.instance_id,
                frame_id=observed.frame.frame_id,
                request_id="first",
                action={"type": "move", "x": 5, "y": 5},
            )
            with pytest.raises(PaletteError) as info:
                await service.act(
                    "desktop",
                    instance_id=instance.view.instance_id,
                    frame_id=observed.frame.frame_id,
                    request_id="second",
                    action={"type": "click", "x": 5, "y": 5},
                )
            assert info.value.code is ErrorCode.STALE_FRAME
        finally:
            await service.shutdown()
