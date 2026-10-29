"""Lifecycle against a real child process.

The fake QEMU is started by the real launcher, over real pipes, and torn down by the real
manager. That is deliberate: a fake QMP object inside the test process would not catch a
missing ``close_fds``, an unread stderr pipe, a leaked reader task or an orphaned process,
and those are exactly the failures the plan's lifecycle acceptance criteria target.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from conftest import write_behavior
from palette.errors import ErrorCode, PaletteError
from palette.service import PaletteService
from palette.vm.manager import VmState
from support.profiles import make_profile


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestStart:
    async def test_a_started_vm_reports_each_readiness_level_honestly(self, running_vm) -> None:
        assert running_vm.state is VmState.RUNNING
        assert running_vm.view.qmp_ready is True
        # A valid framebuffer is progress, but it is not proof of a desktop: firmware and
        # a login prompt look the same from here.
        assert running_vm.view.screen_ready is True
        assert running_vm.view.desktop_ready is None

    async def test_the_display_is_bound_to_the_configured_device(self, running_vm) -> None:
        assert running_vm.view.display_id == "video0"
        assert running_vm.view.head == 0
        assert running_vm.view.native_size.width == 320
        assert running_vm.view.native_size.height == 200

    async def test_the_process_is_alive_and_hidden(self, running_vm) -> None:
        assert running_vm.process is not None
        assert running_vm.process.returncode is None
        assert running_vm.view.instance_id.startswith("inst_")

    async def test_qemu_stderr_is_consumed(self, running_vm) -> None:
        """An unread stderr pipe eventually blocks the guest, so the manager drains it."""
        for _ in range(50):
            if running_vm.stderr_tail(1):
                break
            await asyncio.sleep(0.02)
        assert running_vm.stderr_tail(1)

    async def test_the_command_line_contains_no_window_and_no_network(self, running_vm) -> None:
        argv = running_vm.plan.argv
        assert "-display" in argv and argv[argv.index("-display") + 1] == "none"
        assert "-nic" in argv and argv[argv.index("-nic") + 1] == "none"
        assert any(argument.startswith("VGA,id=video0") for argument in argv)
        assert any("usb-tablet" in argument for argument in argv)
        assert any("usb-kbd" in argument for argument in argv)
        if running_vm.plan.qmp_endpoint is None:
            assert "stdio,id=qmp,signal=off" in argv
        else:
            assert any(argument.startswith("socket,id=qmp,") for argument in argv)
        assert not any("vnc" in argument.lower() for argument in argv)

    async def test_the_overlay_points_at_the_base_image(self, running_vm, base_image: Path) -> None:
        import json

        assert running_vm.paths.overlay.is_file()
        raw = running_vm.paths.overlay.read_bytes()
        assert raw.startswith(b"QFI\xfb")
        chain = json.loads(raw[4:].decode("utf-8"))
        # The backing file is recorded explicitly, so the chain cannot be reinterpreted by
        # renaming a file or by auto-probing an extension.
        assert Path(chain["backing"]) == base_image
        assert chain["backing_format"] == "qcow2"

    async def test_a_manifest_records_the_instance(self, running_vm) -> None:
        manifest = running_vm.paths.manifest
        assert manifest.is_file()
        import json

        document = json.loads(manifest.read_text(encoding="utf-8"))
        assert document["vm_id"] == "desktop"
        assert document["instance_id"] == running_vm.view.instance_id
        assert document["state"] == "RUNNING"

    async def test_starting_twice_is_refused(self, service: PaletteService, running_vm) -> None:
        with pytest.raises(PaletteError) as info:
            await service.manager.start("desktop", "default")
        assert info.value.code is ErrorCode.VM_ALREADY_RUNNING

    async def test_an_unknown_vm_is_reported_not_created(self, service: PaletteService) -> None:
        with pytest.raises(PaletteError) as info:
            await service.manager.require_running("nope")
        assert info.value.code is ErrorCode.VM_NOT_FOUND
        assert info.value.details["known_vms"] == []

    async def test_a_missing_base_image_fails_before_anything_is_created(
        self, service: PaletteService, config, base_image: Path, tmp_path: Path
    ) -> None:
        broken = make_profile(tmp_path / "does-not-exist.qcow2")
        service.config.profiles["default"] = broken  # type: ignore[index]
        with pytest.raises(PaletteError) as info:
            await service.manager.start("desktop", "default")
        assert info.value.code is ErrorCode.IMAGE_MISSING
        # Nothing was left behind: no half-built instance directory, no tracked VM. (The
        # empty per-VM parent directory may remain; it holds no data and no process.)
        assert service.manager.find("desktop") is None
        instances = service.config.runtime_root / "vms" / "desktop"
        assert not instances.exists() or not list(instances.iterdir())

    async def test_a_failed_start_is_not_reported_as_running(
        self, service: PaletteService, tmp_path: Path
    ) -> None:
        service.config.profiles["default"] = make_profile(tmp_path / "missing.qcow2")  # type: ignore[index]
        with pytest.raises(PaletteError):
            await service.manager.start("desktop", "default")
        status = await service.status("desktop")
        assert status.ok is False


class TestStop:
    async def test_a_normal_stop_ends_the_process(
        self, service: PaletteService, running_vm
    ) -> None:
        process = running_vm.process
        await service.manager.stop("desktop")
        assert process is not None and process.returncode is not None
        assert service.manager.get("desktop").state is VmState.STOPPED

    async def test_a_guest_that_ignores_powerdown_is_killed(self, service: PaletteService) -> None:
        """system_powerdown is a request, never a guarantee. The forced path is what makes
        a stop truthful: returning while QEMU still runs would be a lie."""
        instance = await service.manager.start("desktop", "default")
        process = instance.process
        write_behavior(instance, {"ignore_powerdown": True})
        await service.manager.stop("desktop")
        assert process is not None and process.returncode is not None
        assert service.manager.get("desktop").state is VmState.STOPPED

    async def test_stopping_an_unknown_vm_is_refused(self, service: PaletteService) -> None:
        with pytest.raises(PaletteError) as info:
            await service.manager.stop("ghost")
        assert info.value.code is ErrorCode.VM_NOT_FOUND

    async def test_emergency_stop_kills_without_waiting(self, service: PaletteService) -> None:
        instance = await service.manager.start("desktop", "default")
        process = instance.process
        stopped = await service.emergency_stop("desktop")
        assert stopped == ["desktop"]
        assert process is not None and process.returncode is not None
        assert service.manager.get("desktop").state is VmState.STOPPED


class TestReset:
    async def test_a_reset_produces_a_new_instance_and_boot_epoch(
        self, service: PaletteService, running_vm
    ) -> None:
        first = running_vm.view.instance_id
        first_epoch = running_vm.view.boot_epoch
        renewed = await service.manager.reset("desktop")
        assert renewed.view.instance_id != first
        assert renewed.view.boot_epoch == first_epoch + 1
        assert renewed.state is VmState.RUNNING
        assert renewed.view.action_seq == 0

    async def test_the_base_image_is_never_written_to(
        self, service: PaletteService, base_image: Path
    ) -> None:
        before = sha256(base_image)
        await service.manager.start("desktop", "default")
        await service.manager.reset("desktop")
        await service.manager.stop("desktop", force=True)
        assert sha256(base_image) == before

    async def test_the_previous_overlay_is_kept_by_the_retention_policy(
        self, service: PaletteService, running_vm
    ) -> None:
        first_instance = running_vm.view.instance_id
        first_overlay = running_vm.paths.overlay
        await service.manager.reset("desktop")
        # The plan keeps the stopped layer so its results can be retrieved; it is never
        # committed back into the base.
        assert first_overlay.is_file()
        instances = sorted(
            path.name for path in (service.config.runtime_root / "vms" / "desktop").iterdir()
        )
        assert first_instance in instances
        assert len(instances) == 2

    async def test_reset_fences_every_frame_from_before(
        self, service: PaletteService, running_vm
    ) -> None:
        observed = await service.observe("desktop")
        assert observed.frame is not None
        await service.manager.reset("desktop")
        current = service.manager.get("desktop")
        assert current.view.instance_id != observed.frame.instance_id


class TestRepeatedLifecycle:
    async def test_twenty_start_stop_cycles_leave_no_orphans(
        self, service: PaletteService, base_image: Path
    ) -> None:
        """The plan's P1 acceptance criterion: 20 cycles, no orphaned processes and an
        unchanged base image."""
        before = sha256(base_image)
        processes = []
        for _ in range(20):
            instance = await service.manager.start("desktop", "default")
            assert instance.state is VmState.RUNNING
            processes.append(instance.process)
            await service.manager.stop("desktop")
            assert instance.state is VmState.STOPPED
        for process in processes:
            assert process is not None
            assert process.returncode is not None, "a QEMU process outlived its VM"
        assert sha256(base_image) == before

    async def test_repeated_resets_stay_bounded(
        self, service: PaletteService, base_image: Path
    ) -> None:
        before = sha256(base_image)
        for _ in range(6):
            await service.manager.reset("desktop")
        await service.manager.stop("desktop", force=True)
        instance_dirs = list((service.config.runtime_root / "vms" / "desktop").iterdir())
        # The retention limit is honoured rather than accumulating forever.
        assert len(instance_dirs) <= service.config.profile("default").retention.max_overlays + 1
        assert sha256(base_image) == before


class TestShutdown:
    async def test_shutdown_stops_everything(self, service: PaletteService) -> None:
        instance = await service.manager.start("desktop", "default")
        process = instance.process
        await service.shutdown()
        assert process is not None and process.returncode is not None

    async def test_shutdown_is_idempotent(self, service: PaletteService) -> None:
        await service.manager.start("desktop", "default")
        await service.shutdown()
        await service.shutdown()
