"""Opt-in QEMU firmware acceptance for bounded shutdown and explicit force recovery."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from palette.audit import NullAuditLog
from palette.config import HostConfig, PaletteConfig, Profile
from palette.desktop import DesktopManager
from palette.errors import ErrorCode, PaletteError
from palette.service import PaletteService
from palette.vm.manager import VmState

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not os.environ.get("PALETTE_SHUTDOWN_QEMU"), reason="set PALETTE_SHUTDOWN_QEMU"
    ),
]


async def test_real_unresponsive_guest_force_interrupt_and_broken_qmp_recovery(tmp_path):
    qemu = Path(os.environ["PALETTE_SHUTDOWN_QEMU"]).resolve()
    base = tmp_path / "halt.raw"
    base.write_bytes((b"\xfa\xf4\xeb\xfd".ljust(510, b"\0") + b"\x55\xaa").ljust(1024**2, b"\0"))
    original = base.read_bytes()
    profile = Profile.model_validate(
        {
            "profile_id": "shutdown",
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
            "timeouts": {"shutdown_seconds": 0.3, "force_kill_seconds": 2},
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
    normal = None
    try:
        vm_id = (
            await desktop.create(
                {
                    "name": "Real shutdown",
                    "profile_id": profile.profile_id,
                    "source": "image",
                    "path": str(base),
                    "image_format": "raw",
                    "vcpus": 1,
                    "memory_mib": 256,
                    "disk_gib": 1,
                    "network": "offline",
                }
            )
        )["vm_id"]
        await desktop.start(vm_id)
        instance = service.manager.get(vm_id)
        disk = instance.paths.overlay
        await instance.qmp.execute("stop")
        instance.view.state = "PAUSED"
        with pytest.raises(PaletteError) as failed:
            await desktop.stop(vm_id)
        assert failed.value.code is ErrorCode.TIMEOUT
        assert instance.state is VmState.RUNNING
        instance.profile = instance.profile.model_copy(
            update={
                "timeouts": instance.profile.timeouts.model_copy(update={"shutdown_seconds": 60})
            }
        )
        normal = asyncio.create_task(desktop.stop(vm_id))
        async with asyncio.timeout(3):
            while instance.state is not VmState.STOPPING:  # noqa: ASYNC110 - lifecycle observation
                await asyncio.sleep(0.01)
        await asyncio.wait_for(
            desktop.stop(vm_id, force=True, expected_instance_id=instance.view.instance_id), 5
        )
        await asyncio.wait_for(normal, 1)
        assert instance.process.returncode is not None and instance.state is VmState.STOPPED
        assert disk.exists() and base.read_bytes() == original
        await desktop.start(vm_id)
        instance = service.manager.get(vm_id)
        assert instance.paths.overlay == disk
        await instance.qmp.close()
        assert (await desktop.list())[0]["status"]["process_running"]
        await asyncio.wait_for(
            desktop.stop(vm_id, force=True, expected_instance_id=instance.view.instance_id), 5
        )
        assert instance.process.returncode is not None and instance.state is VmState.STOPPED
        assert base.read_bytes() == original
        info = await service.manager.images_for(profile).inspect_image(disk)
        assert info.chain == (disk, base)
    finally:
        if normal is not None and not normal.done():
            normal.cancel()
            await asyncio.gather(normal, return_exceptions=True)
        if vm_id and service.manager.find(vm_id):
            await desktop.stop(vm_id, force=True)
        await service.shutdown()
