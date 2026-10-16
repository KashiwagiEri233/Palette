"""Opt-in Windows QEMU acceptance for native-codepage media and disk paths.

Enable with PALETTE_E2E=1 and PALETTE_E2E_QEMU pointing to the real emulator.
Only temporary, empty media are used; no guest OS image is required.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from palette.config import load_config
from palette.qmp.client import QmpClient
from palette.qmp.transport import ProcessQmpTransport
from palette.vm.images import resolve_qemu_img
from palette.vm.launcher import ExtraVolume, build_launch_plan, run_tool, spawn_qemu
from palette.vm.managed import terminate_process_group

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        sys.platform != "win32"
        or not (os.environ.get("PALETTE_E2E") and os.environ.get("PALETTE_E2E_QEMU")),
        reason="requires Windows, PALETTE_E2E=1 and PALETTE_E2E_QEMU",
    ),
]


async def test_real_qemu_opens_chinese_paths_without_changing_media_flags(tmp_path: Path) -> None:
    profile = load_config(Path(__file__).resolve().parents[2]).profile(None)
    binary = os.environ["PALETTE_E2E_QEMU"]
    profile = profile.model_copy(
        update={"qemu": profile.qemu.model_copy(update={"binary": binary})}
    )
    qemu_img = resolve_qemu_img(profile)
    assert qemu_img is not None
    directory = tmp_path / "中文目录"
    directory.mkdir()
    overlay = directory / "磁盘,测试.qcow2"
    media = directory / "安装 盘,readonly=off.iso"
    media.write_bytes(b"\0" * (1024 * 1024))
    await run_tool([qemu_img, "create", "-f", "qcow2", str(overlay), "16M"])
    plan = build_launch_plan(
        profile=profile,
        binary=binary,
        instance_id="inst_unicode",
        vm_id="unicode",
        overlay_path=overlay,
        workdir=tmp_path,
        extra_volumes=(ExtraVolume(path=str(media), media="cdrom", boot_index=0),),
    )
    process = await spawn_qemu(plan)
    qmp = QmpClient(ProcessQmpTransport(process))
    try:
        await qmp.start()
        assert (await qmp.execute("query-status"))["running"] is False
        blocks = await qmp.execute("query-block")
        disk = next(block["inserted"] for block in blocks if block["device"] == "disk")
        optical = next(block["inserted"] for block in blocks if block["device"] == "extra0")
        assert disk["drv"] == "qcow2" and disk["ro"] is False
        assert disk["image"]["virtual-size"] == 16 * 1024 * 1024
        assert optical["drv"] == "raw" and optical["ro"] is True
        assert optical["image"]["virtual-size"] == media.stat().st_size
        await qmp.execute("quit")
        assert await asyncio.wait_for(process.wait(), 10) == 0
    finally:
        if process.returncode is None:
            assert await asyncio.to_thread(terminate_process_group, process.pid)
            await asyncio.wait_for(process.wait(), 10)
        await qmp.close()
    assert media.read_bytes() == b"\0" * (1024 * 1024)
