"""Opt-in real QEMU guest sockets and repeated ACPI shutdown/start acceptance.

PALETTE_QMP_QEMU enables the firmware/socket checks. Also supply
PALETTE_QMP_ALPINE_ISO, PALETTE_QMP_KERNEL and PALETTE_QMP_INITRD for real Alpine
ACPI shutdown cycles. Images are read-only sources for a disposable persistent VM.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import tempfile
import time
from pathlib import Path

import pytest

from palette.audit import NullAuditLog
from palette.config import HostConfig, PaletteConfig, Profile
from palette.desktop import DesktopManager
from palette.errors import ErrorCode, PaletteError
from palette.guest_channel import GuestChannel
from palette.qmp.client import QmpClient
from palette.service import PaletteService
from palette.vm import manager as manager_module
from palette.vm.launcher import LaunchOverrides, run_tool
from palette.vm.manager import VmState

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not os.environ.get("PALETTE_QMP_QEMU"), reason="set PALETTE_QMP_QEMU"),
]


def configuration(root, base):
    qemu = Path(os.environ["PALETTE_QMP_QEMU"]).resolve()
    accelerator = os.environ.get("PALETTE_QMP_ACCEL", "whpx" if os.name == "nt" else "tcg")
    profile = Profile.model_validate(
        {
            "profile_id": "restart",
            "qemu": {
                "binary": str(qemu),
                "qemu_img": str(qemu.with_name("qemu-img" + qemu.suffix)),
                "accelerator": accelerator,
                "machine": "q35",
                "cpu": "max" if accelerator == "tcg" else None,
            },
            "images": {"base": str(base), "format": "raw", "size_gib": 1},
            "resources": {"vcpus": 1, "memory_mib": 512, "process_memory_limit_mib": None},
            "network": {"mode": "offline"},
            "guest_agent": {"enabled": True},
            "guest_tools": {"enabled": True},
            "guest_desktop": {"clipboard": False, "resize": True, "shared_folders": True},
            "timeouts": {
                "startup_seconds": 45,
                "qmp_handshake_seconds": 5,
                "shutdown_seconds": 20,
                "boot_poll_seconds": 0.1,
            },
        }
    )
    return PaletteConfig(
        profiles={profile.profile_id: profile},
        config_root=root,
        host=HostConfig(default_profile=profile.profile_id, runtime_root=str(root / "runtime")),
    )


@pytest.mark.parametrize("absent_peers", [False, True])
async def test_real_four_guest_sockets_do_not_block_qmp(tmp_path, monkeypatch, absent_peers):
    config = configuration(tmp_path, tmp_path / "unused.raw")
    if absent_peers:

        async def never_connect(channel, **kwargs):
            await asyncio.Event().wait()

        monkeypatch.setattr(GuestChannel, "open", never_connect)
    service = PaletteService(config, audit=NullAuditLog())
    await service.startup()
    directories = set()
    try:
        for _ in range(5):
            instance = await service.manager.start(
                "socket-test", overrides=LaunchOverrides(create_overlay=False, attach_primary=False)
            )
            assert instance.view.qmp_ready and instance.view.screen_ready
            assert len(service.manager._guest_channel_report(instance)) == 4
            directory = instance.plan.guest_ipc_directory
            assert directory is None or directory not in directories
            directories.add(directory)
            await service.manager.stop("socket-test", force=True)
            assert instance.process.returncode is not None
            assert directory is None or not directory.exists()
    finally:
        await service.shutdown()


async def test_real_qmp_concurrent_input_and_status_queries(tmp_path, record_property):
    service = PaletteService(configuration(tmp_path, tmp_path / "unused.raw"), audit=NullAuditLog())
    await service.startup()
    try:
        instance = await service.manager.start(
            "concurrent-input",
            overrides=LaunchOverrides(create_overlay=False, attach_primary=False),
        )
        lease = await service.control.acquire("concurrent-input", instance.view.instance_id)

        async def inputs():
            for seq in range(1, 501):
                size = instance.view.native_size
                await service.control.input(
                    "concurrent-input",
                    lease["lease_id"],
                    seq,
                    instance.view.display_epoch,
                    size.width,
                    size.height,
                    [
                        {"type": "move", "x": seq % size.width, "y": seq % size.height},
                        {"type": "key", "key": "a", "down": True},
                        {"type": "key", "key": "a", "down": False},
                    ],
                )
                if seq % 50 == 0:
                    service.control.heartbeat("concurrent-input", lease["lease_id"])

        async def queries():
            for _ in range(50):
                await asyncio.gather(
                    *(instance.qmp.execute("query-status", timeout=3) for _ in range(32))
                )

        async with asyncio.timeout(90):
            await asyncio.gather(inputs(), queries())
        assert instance.qmp.diagnostics()["protocol_errors"] == []
        assert instance.controller.ledger_snapshot() == {"keys": [], "buttons": []}
        await service.control.release("concurrent-input", lease["lease_id"])
        record_property("input_batches", 500)
        record_property("parallel_status_queries", 1600)
        record_property("transport", instance.qmp.diagnostics()["transport"])
    finally:
        await service.manager.stop("concurrent-input", force=True)
        await service.shutdown()


@pytest.mark.skipif(os.name != "nt", reason="Windows endpoint adapter")
async def test_real_unicode_guest_socket_endpoints(tmp_path, monkeypatch):
    from palette import windows_unix

    with tempfile.TemporaryDirectory(prefix="pq-") as temporary:
        temporary_root = Path(temporary).resolve()
        assert temporary_root.parent == Path(tempfile.gettempdir()).resolve()
        directory = temporary_root / "Palette" / "ipc" / "中文 空格"
        monkeypatch.setattr(windows_unix, "endpoint_directory", lambda: directory)
        service = PaletteService(
            configuration(tmp_path, tmp_path / "unused.raw"), audit=NullAuditLog()
        )
        await service.startup()
        try:
            instance = await service.manager.start(
                "unicode-sockets",
                overrides=LaunchOverrides(create_overlay=False, attach_primary=False),
            )
            async with asyncio.timeout(3):
                while not all(  # noqa: ASYNC110 - observe independent channel connectors
                    item["connected"]
                    for item in service.manager._guest_channel_report(instance).values()
                ):
                    await asyncio.sleep(0.01)
            assert instance.view.qmp_ready
            await service.manager.stop("unicode-sockets", force=True)
            assert not directory.exists()
        finally:
            await service.shutdown()


async def test_real_failed_start_cleans_endpoints_before_retry(tmp_path, monkeypatch):
    from palette import windows_unix

    directories = []
    original_directory = windows_unix.endpoint_directory

    def tracked_directory():
        directory = original_directory()
        directories.append(directory)
        return directory

    async def fail_handshake(client):
        raise PaletteError(ErrorCode.QMP_UNAVAILABLE, "injected greeting failure")

    monkeypatch.setattr(windows_unix, "endpoint_directory", tracked_directory)
    service = PaletteService(configuration(tmp_path, tmp_path / "unused.raw"), audit=NullAuditLog())
    await service.startup()
    overrides = LaunchOverrides(create_overlay=False, attach_primary=False)
    try:
        with monkeypatch.context() as failure:
            failure.setattr(QmpClient, "start", fail_handshake)
            with pytest.raises(PaletteError) as error:
                await service.manager.start("retry-sockets", overrides=overrides)
            assert error.value.code is ErrorCode.STARTUP_FAILED
        assert all(not directory.exists() for directory in directories)
        instance = await service.manager.start("retry-sockets", overrides=overrides)
        assert instance.view.qmp_ready
        assert len(directories) == len(set(directories))
        await service.manager.stop("retry-sockets", force=True)
        assert all(not directory.exists() for directory in directories)
    finally:
        await service.shutdown()


@pytest.mark.skipif(
    not all(
        os.environ.get(name)
        for name in ("PALETTE_QMP_ALPINE_ISO", "PALETTE_QMP_KERNEL", "PALETTE_QMP_INITRD")
    ),
    reason="set PALETTE_QMP_ALPINE_ISO, PALETTE_QMP_KERNEL and PALETTE_QMP_INITRD",
)
async def test_real_persistent_guest_normal_shutdown_then_immediate_start(
    tmp_path, monkeypatch, record_property
):
    iso = Path(os.environ["PALETTE_QMP_ALPINE_ISO"]).resolve()
    config = configuration(tmp_path, iso)
    connections = asyncio.Queue()

    def connected(reader, writer):
        connections.put_nowait((reader, writer))

    server = await asyncio.start_server(connected, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    profile = config.profile(None)
    profile = profile.model_copy(
        update={
            "qemu": profile.qemu.model_copy(
                update={
                    "extra_args": [
                        "-kernel",
                        str(Path(os.environ["PALETTE_QMP_KERNEL"]).resolve()),
                        "-initrd",
                        str(Path(os.environ["PALETTE_QMP_INITRD"]).resolve()),
                        "-append",
                        "modules=loop,squashfs,sd-mod,usb-storage console=ttyS0,115200",
                        "-serial",
                        f"tcp:127.0.0.1:{port}",
                    ]
                }
            )
        }
    )
    config.profiles[profile.profile_id] = profile
    original_plan = manager_module.build_launch_plan

    def kernel_plan(**kwargs):
        plan = original_plan(**kwargs)
        # QEMU reserves bootindex 0 for direct kernel boot.
        plan.argv = [
            re.sub(r"bootindex=(\d+)", lambda match: f"bootindex={int(match[1]) + 2}", arg)
            for arg in plan.argv
        ]
        return plan

    monkeypatch.setattr(manager_module, "build_launch_plan", kernel_plan)
    service = PaletteService(config, audit=NullAuditLog())
    desktop = DesktopManager(service)
    cycles = int(os.environ.get("PALETTE_QMP_CYCLES", "20"))
    timings = []
    identities = set()
    disk = None
    qemu = Path(os.environ["PALETTE_QMP_QEMU"]).resolve()
    qemu_io = qemu.with_name("qemu-io" + qemu.suffix)
    source_hash = hashlib.sha256(iso.read_bytes()).hexdigest()
    writer = None
    await service.startup()
    try:
        vm_id = (
            await desktop.create(
                {
                    "name": "QMP restart acceptance",
                    "profile_id": profile.profile_id,
                    "source": "iso",
                    "path": str(iso),
                    "vcpus": 1,
                    "memory_mib": 512,
                    "disk_gib": 1,
                    "network": "offline",
                }
            )
        )["vm_id"]
        for cycle in range(cycles + 2):
            started = time.monotonic()
            await desktop.start(vm_id)
            instance = service.manager.get(vm_id)
            timings.append(time.monotonic() - started)
            assert instance.view.instance_id not in identities
            identities.add(instance.view.instance_id)
            if disk is None:
                disk = instance.paths.overlay
            assert instance.paths.overlay == disk
            directory = instance.plan.guest_ipc_directory
            reader, writer = await asyncio.wait_for(connections.get(), 20)
            async with asyncio.timeout(90):
                await reader.readuntil(b"login:")
                writer.write(b"root\n")
                await writer.drain()
                await reader.readuntil(b"localhost:~#")
                writer.write(b"rc-service acpid start && echo PALETTE_ACPID_READY\n")
                await writer.drain()
                output = await reader.readuntil(b"localhost:~#")
                assert b"\r\nPALETTE_ACPID_READY\r\n" in output, output[-2000:]
                # OpenRC reports a background daemon started before it has necessarily
                # entered its ACPI event loop. Allow it to subscribe before powerdown.
                await asyncio.sleep(1)
            if cycle < cycles:
                try:
                    await desktop.stop(vm_id)
                except Exception:
                    writer.write(b"echo PALETTE_SHUTDOWN_DIAGNOSTICS; ps; cat /proc/interrupts\n")
                    await writer.drain()
                    with contextlib.suppress(TimeoutError, OSError, asyncio.IncompleteReadError):
                        async with asyncio.timeout(3):
                            output = await reader.readuntil(b"localhost:~#")
                            print(repr(output[-6000:]), flush=True)
                    raise
                assert instance.guest_shutdown, "normal shutdown must be confirmed by QEMU"
            elif cycle == cycles:
                writer.write(b"poweroff\n")
                await writer.drain()
                await asyncio.wait_for(instance.process.wait(), 20)
                async with asyncio.timeout(5):
                    while instance.state is not VmState.STOPPED:  # noqa: ASYNC110 - lifecycle observation
                        await asyncio.sleep(0.01)
            else:
                await desktop.stop(vm_id, force=True)
            assert instance.process.returncode is not None
            assert directory is None or not directory.exists()
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            writer = None
            command = "write -P 0x5a 1048576 4096" if cycle == 0 else "read -P 0x5a 1048576 4096"
            code, output, error = await run_tool(
                [str(qemu_io), "-f", "qcow2", "-c", command, str(disk)]
            )
            assert code == 0, output + error
            print(f"cycle {cycle + 1}/{cycles + 2}: start={timings[-1]:.3f}s", flush=True)
        assert hashlib.sha256(iso.read_bytes()).hexdigest() == source_hash
        record_property("normal_shutdown_cycles", cycles)
        record_property("startup_seconds", timings)
        record_property("accelerator", profile.qemu.accelerator)
    finally:
        if writer is not None:
            writer.close()
        await service.shutdown()
        server.close()
        await server.wait_closed()
