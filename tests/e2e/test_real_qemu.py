"""Physical acceptance using the selected native or explicit TCG profile.

Skipped unless an operator opts in, because the plan's P0 gate cannot be faked: proving
that input lands in the right device and that a screenshot comes back without a host
window requires the actual emulator and a real desktop image.

Enable with::

    set PALETTE_E2E=1
    set PALETTE_E2E_PROFILE=tcg-x86_64

QEMU, IMAGE and ACCEL overrides remain available through PALETTE_E2E_* variables.
Without PROFILE, overrides use the configured default profile's guest architecture.
JUnit properties record the execution target and timings separately for each run.

Nothing here is claimed to have passed: a skipped test is not evidence.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from palette.audit import NullAuditLog
from palette.cli import build_doctor_report
from palette.config import HostConfig, PaletteConfig, Profile, load_config
from palette.errors import ErrorCode
from palette.platforms import execution_info
from palette.service import PaletteService

pytestmark = pytest.mark.e2e

QEMU = os.environ.get("PALETTE_E2E_QEMU")
IMAGE = os.environ.get("PALETTE_E2E_IMAGE")
ACCEL = os.environ.get("PALETTE_E2E_ACCEL")
PROFILE = os.environ.get("PALETTE_E2E_PROFILE")

requires_hardware = pytest.mark.skipif(
    not (os.environ.get("PALETTE_E2E") and (PROFILE or (QEMU and IMAGE))),
    reason="set PALETTE_E2E=1 and PALETTE_E2E_PROFILE (or QEMU and IMAGE overrides)",
)


def real_profile() -> Profile:
    profile = load_config(Path(__file__).resolve().parents[2]).profile(PROFILE)
    data = profile.model_dump()
    if QEMU:
        data["qemu"]["binary"] = QEMU
    if IMAGE:
        data["images"]["base"] = IMAGE
    if ACCEL:
        data["qemu"]["accelerator"] = ACCEL
        data["qemu"]["cpu"] = "max" if ACCEL == "tcg" else "host"
    return Profile.model_validate(data)


def real_config(tmp_path: Path) -> PaletteConfig:
    profile = real_profile()
    return PaletteConfig(
        profiles={profile.profile_id: profile},
        config_root=tmp_path,
        host=HostConfig(default_profile=profile.profile_id, runtime_root=str(tmp_path / "runtime")),
    )


def base_digest() -> str:
    digest = hashlib.sha256()
    with open(real_profile().images.base, "rb") as handle:
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
        assert real_profile().qemu.accelerator in report["acceleration"]["available"]

    async def test_a_hidden_start_reaches_a_real_framebuffer(
        self, tmp_path: Path, record_property
    ) -> None:
        service = PaletteService(real_config(tmp_path), audit=NullAuditLog())
        await service.startup()
        try:
            started = time.perf_counter()
            instance = await service.manager.start("desktop")
            record_property("execution", execution_info(instance.profile))
            record_property("startup_seconds", time.perf_counter() - started)
            assert instance.view.qmp_ready is True
            assert instance.view.screen_ready is True, "no valid framebuffer was captured"
            assert instance.view.native_size is not None
            result, image = await service.observe_image("desktop")
            assert result.ok is True
            assert image.startswith(b"\x89PNG")
            Image.open(BytesIO(image)).verify()
        finally:
            await service.shutdown()

    async def test_see_click_type_see(self, tmp_path: Path) -> None:
        """The P0 pass criterion: look, act, and see the result, with no host focus."""
        service = PaletteService(real_config(tmp_path), audit=NullAuditLog())
        await service.startup()
        try:
            instance = await service.manager.start("desktop")
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

            typed, _ = await service.act(
                "desktop",
                instance_id=instance.view.instance_id,
                frame_id=receipt.frame.frame_id if receipt.frame else observed.frame.frame_id,
                request_id="p0-text",
                action={"type": "type_text", "text": "palette", "mode": "keyboard"},
            )
            assert typed.ok is True, typed.error
        finally:
            await service.shutdown()

    async def test_twenty_cycles_leave_the_base_image_untouched(self, tmp_path: Path) -> None:
        before = base_digest()
        service = PaletteService(real_config(tmp_path), audit=NullAuditLog())
        await service.startup()
        try:
            for _ in range(20):
                await service.manager.start("desktop")
                await service.manager.stop("desktop")
        finally:
            await service.shutdown()
        assert base_digest() == before

    async def test_a_stale_frame_is_refused_on_hardware(self, tmp_path: Path) -> None:
        service = PaletteService(real_config(tmp_path), audit=NullAuditLog())
        await service.startup()
        try:
            instance = await service.manager.start("desktop")
            observed = await service.observe("desktop")
            assert observed.frame is not None
            await service.act(
                "desktop",
                instance_id=instance.view.instance_id,
                frame_id=observed.frame.frame_id,
                request_id="first",
                action={"type": "move", "x": 5, "y": 5},
            )
            rejected, _ = await service.act(
                "desktop",
                instance_id=instance.view.instance_id,
                frame_id=observed.frame.frame_id,
                request_id="second",
                action={"type": "click", "x": 5, "y": 5},
            )
            assert not rejected.ok and rejected.error.code == ErrorCode.STALE_FRAME
        finally:
            await service.shutdown()

    async def test_reset_and_cold_reset_preserve_base(self, tmp_path, record_property):
        before = base_digest()
        service = PaletteService(real_config(tmp_path), audit=NullAuditLog())
        await service.startup()
        try:
            instance = await service.manager.start("desktop")
            observed = await service.observe("desktop")
            epoch = instance.view.boot_epoch
            await instance.qmp.execute("system_reset")
            async with asyncio.timeout(instance.profile.timeouts.startup_seconds):
                while instance.view.boot_epoch == epoch:  # noqa: ASYNC110 - QMP event updates the epoch
                    await asyncio.sleep(0.05)
            rejected, _ = await service.act(
                "desktop",
                instance_id=instance.view.instance_id,
                frame_id=observed.frame.frame_id,
                request_id="pre-reset-frame",
                action={"type": "click", "x": 10, "y": 10},
            )
            assert not rejected.ok
            started = time.perf_counter()
            replacement = await service.manager.reset("desktop")
            record_property("cold_reset_seconds", time.perf_counter() - started)
            record_property("execution", execution_info(replacement.profile))
            assert replacement.view.instance_id != instance.view.instance_id
            result, image = await service.observe_image("desktop")
            assert result.ok
            Image.open(BytesIO(image)).verify()
        finally:
            await service.shutdown()
        assert base_digest() == before
