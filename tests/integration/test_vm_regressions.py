"""Lifecycle transactions, image dependencies and cancellation against real children."""

from __future__ import annotations

import asyncio
import json

import pytest

from palette.config import GuestToolsConfig, RetentionConfig
from palette.errors import ErrorCode, PaletteError
from palette.guest_tools.bridge import MemoryGuestToolTransport
from palette.guest_tools.protocol import PROTOCOL_VERSION
from palette.service import PaletteService
from palette.vm.images import ImageManager
from palette.vm.launcher import LaunchOverrides
from palette.vm.managed import SupervisedProcess
from palette.vm.manager import VmState


def set_retention(service: PaletteService, *, keep: bool, count: int = 1) -> None:
    profile = service.config.profile(None)
    service.config.profiles[profile.profile_id] = profile.model_copy(
        update={"retention": RetentionConfig(keep_overlays=keep, max_overlays=count)}
    )


async def test_failed_channel_replacement_reaps_the_old_process(service: PaletteService) -> None:
    old = await service.manager.start("desktop")
    assert old.process is not None and old.process.stdout is not None
    old.process.stdout.set_exception(ConnectionResetError("injected disconnect"))
    for _ in range(100):
        if old.state is VmState.FAILED:
            break
        await asyncio.sleep(0.01)
    assert old.state is VmState.FAILED
    assert old.process.returncode is None
    current = await service.manager.start("desktop")
    assert old.process.returncode is not None
    assert not old.tasks
    assert old.controller is not None and old.controller._worker is None
    assert current is service.manager.get("desktop")
    assert current.process is not None and current.process.returncode is None


async def test_cancel_during_overlay_creation_can_be_retried(service, monkeypatch) -> None:
    entered = asyncio.Event()

    async def pending_overlay(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    with monkeypatch.context() as patch:
        patch.setattr(ImageManager, "create_overlay", pending_overlay)
        task = asyncio.create_task(service.manager.start("desktop"))
        await asyncio.wait_for(entered.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert service.manager.find("desktop") is None
    assert not list((service.config.runtime_root / "vms" / "desktop").iterdir())
    assert (await service.manager.start("desktop")).state is VmState.RUNNING


async def test_startup_budget_includes_overlay_creation(service, monkeypatch) -> None:
    profile = service.config.profile(None)
    service.config.profiles[profile.profile_id] = profile.model_copy(
        update={"timeouts": profile.timeouts.model_copy(update={"startup_seconds": 0.1})}
    )

    async def pending_overlay(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(ImageManager, "create_overlay", pending_overlay)
    with pytest.raises(PaletteError) as error:
        await asyncio.wait_for(service.manager.start("desktop"), 5)
    assert error.value.code is ErrorCode.TIMEOUT
    assert service.manager.find("desktop") is None
    assert not list((service.config.runtime_root / "vms" / "desktop").iterdir())


async def test_failed_process_guard_reaps_the_spawned_process(service, monkeypatch) -> None:
    from palette.vm import manager

    children = []
    spawn = manager.spawn_qemu

    async def recording_spawn(plan, **kwargs):
        child = await spawn(plan, **kwargs)
        children.append(child)
        return child

    class RejectedGuard:
        description = "injected attachment failure"

        def attach(self, pid):
            return False

        def close(self):
            pass

    monkeypatch.setattr(manager, "spawn_qemu", recording_spawn)
    monkeypatch.setattr(manager, "create_process_guard", lambda **kwargs: RejectedGuard())
    monkeypatch.setattr(SupervisedProcess, "attach", lambda self, pid: False)
    with pytest.raises(PaletteError) as error:
        await service.manager.start("desktop")
    assert error.value.code is ErrorCode.STARTUP_FAILED
    assert len(children) == 1 and children[0].returncode is not None
    assert service.manager.find("desktop") is None


async def test_cancelled_start_finishes_cleanup_after_second_cancel(service, monkeypatch) -> None:
    ready = asyncio.Event()
    cleaning = asyncio.Event()
    finish = asyncio.Event()
    teardown = service.manager._teardown

    async def pending_readiness(instance):
        ready.set()
        await asyncio.Event().wait()

    async def delayed_teardown(instance, **kwargs):
        cleaning.set()
        await finish.wait()
        await teardown(instance, **kwargs)

    monkeypatch.setattr(service.manager, "_poll_readiness", pending_readiness)
    monkeypatch.setattr(service.manager, "_teardown", delayed_teardown)
    task = asyncio.create_task(service.manager.start("desktop"))
    await asyncio.wait_for(ready.wait(), 10)
    instance = service.manager.get("desktop")
    task.cancel()
    await asyncio.wait_for(cleaning.wait(), 10)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert instance.process is not None and instance.process.returncode is not None
    assert instance.controller is not None and instance.controller._worker is None
    assert service.manager.find("desktop") is None
    assert not instance.paths.root.exists()


@pytest.mark.parametrize("operation", ["stop", "reset"])
async def test_stop_and_reset_finish_stopping_before_propagating_cancel(
    service, monkeypatch, operation
) -> None:
    set_retention(service, keep=False)
    instance = await service.manager.start("desktop")
    entered = asyncio.Event()
    finish = asyncio.Event()
    original = service.manager._stop_locked

    async def delayed_stop(current, **kwargs):
        entered.set()
        await finish.wait()
        await original(current, **kwargs)

    monkeypatch.setattr(service.manager, "_stop_locked", delayed_stop)
    task = asyncio.create_task(getattr(service.manager, operation)("desktop"))
    await asyncio.wait_for(entered.wait(), 10)
    task.cancel()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert service.manager.get("desktop") is instance
    assert instance.state is VmState.STOPPED
    assert instance.process is not None and instance.process.returncode is not None
    assert not instance.paths.root.exists()


async def test_stop_queued_after_reset_targets_the_replacement(service) -> None:
    first = await service.manager.start("desktop")
    async with service.manager._lock:
        reset = asyncio.create_task(service.manager.reset("desktop"))
        await asyncio.sleep(0)
        stop = asyncio.create_task(service.manager.stop("desktop"))
        await asyncio.sleep(0)
    second = await reset
    await stop
    assert second is not first
    assert second.state is VmState.STOPPED
    assert second.process is not None and second.process.returncode is not None


async def test_cancelled_shutdown_still_stops_every_vm(service, monkeypatch) -> None:
    first = await service.manager.start("first")
    second = await service.manager.start("second")
    entered = asyncio.Event()
    finish = asyncio.Event()
    original = service.manager.stop

    async def delayed_stop(vm_id, **kwargs):
        if vm_id == "first":
            entered.set()
            await finish.wait()
        return await original(vm_id, **kwargs)

    monkeypatch.setattr(service.manager, "stop", delayed_stop)
    task = asyncio.create_task(service.manager.shutdown())
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    for instance in (first, second):
        assert instance.state is VmState.STOPPED
        assert instance.process is not None and instance.process.returncode is not None


async def test_authorized_stop_does_not_apply_to_a_replacement(service) -> None:
    first = await service.manager.start("desktop")
    async with service.manager._lock:
        reset = asyncio.create_task(service.manager.reset("desktop"))
        await asyncio.sleep(0)
        stop = asyncio.create_task(service.stop("desktop"))
        await asyncio.sleep(0)
    second = await reset
    with pytest.raises(PaletteError) as error:
        await stop
    assert error.value.code is ErrorCode.STATE_CONFLICT
    assert second.state is VmState.RUNNING
    assert second.view.instance_id != first.view.instance_id


@pytest.mark.parametrize("keep,expected", [(False, 0), (True, 1)])
async def test_start_stop_obeys_retention(service, keep, expected) -> None:
    set_retention(service, keep=keep)
    for _ in range(3):
        await service.manager.start("desktop")
        await service.manager.stop("desktop")
    assert len(list((service.config.runtime_root / "vms" / "desktop").iterdir())) == expected


async def test_retention_preserves_another_guests_backing_layer(service) -> None:
    source = await service.manager.start("source")
    await service.manager.stop("source")
    set_retention(service, keep=False)
    dependent = await service.manager.start(
        "dependent", overrides=LaunchOverrides(image=source.paths.overlay, image_format="qcow2")
    )
    replacement = await service.manager.start("source")
    await service.manager.stop("source")
    assert source.paths.overlay.exists()
    assert not replacement.paths.root.exists()
    await service.manager.stop("dependent")
    assert not dependent.paths.root.exists()
    service.manager._apply_retention("source", service.config.profile(None))
    assert not source.paths.root.exists()


async def test_retention_preserves_transitive_dependencies_of_retained_layers(service) -> None:
    source = await service.manager.start("source")
    await service.manager.stop("source")
    middle = await service.manager.start(
        "middle", overrides=LaunchOverrides(image=source.paths.overlay, image_format="qcow2")
    )
    await service.manager.stop("middle")
    last = await service.manager.start(
        "last", overrides=LaunchOverrides(image=middle.paths.overlay, image_format="qcow2")
    )
    await service.manager.stop("last")
    set_retention(service, keep=False)
    service.manager._apply_retention("source", service.config.profile(None))
    assert source.paths.overlay.exists()
    assert middle.paths.overlay.exists()
    assert last.paths.overlay.exists()


async def test_raw_backing_and_qcow2_extra_disk_keep_actual_formats(service, tmp_path) -> None:
    raw = tmp_path / "base.raw"
    raw.write_bytes(b"raw disk contents")
    extra = tmp_path / "extra.qcow2"
    extra.write_bytes(b'QFI\xfb{"driver":"qcow2"}')
    result = await service.start(
        "desktop", image=str(raw), image_format="raw", extra_disks=[str(extra)]
    )
    instance = service.manager.get("desktop")
    document = json.loads(instance.paths.manifest.read_text())
    assert result.ok
    assert document["base_image"] == str(raw)
    assert document["base_format"] == "raw"
    assert document["base_from_caller"] is True
    assert document["overlay_format"] == "qcow2"
    assert document["attached_volumes"] == [str(extra)]
    assert document["volume_formats"] == ["qcow2"]
    assert instance.overrides.volumes[0].format == "qcow2"
    created = document["created_at"]
    await service.manager.stop("desktop")
    stopped = json.loads(instance.paths.manifest.read_text())
    assert stopped["created_at"] == created
    assert stopped["state"] == "STOPPED" and stopped["process_exited"] is True
    assert stopped["base_image"] == str(raw)
    renewed = await service.manager.reset("desktop")
    reset_manifest = json.loads(renewed.paths.manifest.read_text())
    assert reset_manifest["base_image"] == str(raw)
    assert reset_manifest["volume_formats"] == ["qcow2"]


async def test_raw_profile_still_creates_a_qcow2_overlay(service, tmp_path) -> None:
    raw = tmp_path / "profile.raw"
    raw.write_bytes(b"raw profile")
    profile = service.config.profile(None)
    service.config.profiles[profile.profile_id] = profile.model_copy(
        update={"images": profile.images.model_copy(update={"base": str(raw), "format": "raw"})}
    )
    instance = await service.manager.start("desktop")
    chain = json.loads(instance.paths.overlay.read_bytes()[4:])
    assert chain["driver"] == "qcow2"
    assert chain["backing_format"] == "raw"


async def test_backing_format_is_checked_independently_of_create_arguments(
    service, base_image
) -> None:
    with pytest.raises(PaletteError) as error:
        await service.start("desktop", image=str(base_image), image_format="raw")
    assert error.value.code is ErrorCode.IMAGE_INVALID
    assert service.manager.find("desktop") is None
    assert not list((service.config.runtime_root / "vms" / "desktop").iterdir())


def test_doctor_checks_disk_contents_instead_of_extension(service, tmp_path) -> None:
    renamed = tmp_path / "base.data"
    renamed.write_bytes(b'QFI\xfb{"driver":"qcow2"}')
    profile = service.config.profile(None)
    profile = profile.model_copy(
        update={"images": profile.images.model_copy(update={"base": str(renamed)})}
    )
    report = service.manager._images(profile).verify_base_image()
    assert report["ok"] is True and report["actual_format"] == "qcow2"
    renamed.write_bytes(b"a raw disk, regardless of its suffix")
    report = service.manager._images(profile).verify_base_image()
    assert report["ok"] is False and report["actual_format"] == "raw"


async def test_unconfirmed_exit_never_deletes_the_overlay(service, monkeypatch) -> None:
    set_retention(service, keep=False)
    instance = await service.manager.start("desktop")
    original = service.manager._teardown

    async def unconfirmed(*args, **kwargs):
        raise PaletteError(ErrorCode.TIMEOUT, "injected exit uncertainty")

    monkeypatch.setattr(service.manager, "_teardown", unconfirmed)
    with pytest.raises(PaletteError):
        await service.manager.stop("desktop")
    assert instance.state is VmState.FAILED
    assert instance.paths.overlay.exists()
    monkeypatch.setattr(service.manager, "_teardown", original)
    await service.manager.stop("desktop", force=True)


async def test_failed_manifest_identity_never_authorizes_deletion(service) -> None:
    instance = await service.manager.start("desktop")
    await service.manager.stop("desktop")
    document = json.loads(instance.paths.manifest.read_text())
    document["instance_id"] = "someone-else"
    instance.paths.manifest.write_text(json.dumps(document))
    images = service.manager._images(instance.profile)
    with pytest.raises(PaletteError) as error:
        images.delete_instance("desktop", instance.view.instance_id)
    assert error.value.code is ErrorCode.PATH_DENIED
    assert instance.paths.overlay.exists()


async def test_qmp_reset_fences_frames_and_reconnects_guest_without_blocking_visual(
    service, monkeypatch
) -> None:
    profile = service.config.profile(None)
    service.config.profiles[profile.profile_id] = profile.model_copy(
        update={"guest_tools": GuestToolsConfig(enabled=True)}
    )
    connecting = asyncio.Event()
    finish = asyncio.Event()
    allow_release = asyncio.Event()
    released = asyncio.Event()
    transports = []
    original_release = service.manager._release_controller

    async def delayed_release(current):
        await allow_release.wait()
        await original_release(current)
        released.set()

    monkeypatch.setattr(service.manager, "_release_controller", delayed_release)

    def responder(message):
        if message["kind"] == "hello":
            return {
                "v": PROTOCOL_VERSION,
                "kind": "hello_ack",
                "id": message["id"],
                "payload": {
                    "version": PROTOCOL_VERSION,
                    "capabilities": ["guest.input_text"],
                    "input_methods": ["native_text"],
                    "interactive_desktop": True,
                },
            }
        return None

    class DelayedTransport(MemoryGuestToolTransport):
        async def open(self):
            connecting.set()
            await finish.wait()
            await super().open()

    def factory(instance):
        transport = (
            DelayedTransport(responder) if transports else MemoryGuestToolTransport(responder)
        )
        transports.append(transport)
        return transport

    service.manager._guest_tool_transport_factory = factory
    instance = await service.manager.start("desktop")
    assert instance.guest_refresh is not None
    await instance.guest_refresh
    old_backend = instance.guest_tools
    assert old_backend is not None and (await old_backend.capabilities()).usable
    observed = await service.observe("desktop")
    assert observed.frame is not None
    epoch = instance.view.boot_epoch
    assert instance.qmp is not None
    await instance.qmp.execute("system_reset")
    await asyncio.wait_for(connecting.wait(), 5)
    assert instance.view.boot_epoch == epoch + 1
    assert instance.guest_tools is not old_backend
    assert not (await old_backend.capabilities()).usable
    assert instance.controller is not None
    with pytest.raises(PaletteError) as error:
        instance.controller._frames.get(observed.frame.frame_id)
    assert error.value.code is ErrorCode.STALE_FRAME
    fresh = await asyncio.wait_for(service.observe("desktop"), 5)
    assert fresh.frame is not None and fresh.frame.boot_epoch == epoch + 1
    allow_release.set()
    await asyncio.wait_for(released.wait(), 5)
    assert instance.controller.frame_by_id(fresh.frame.frame_id) is fresh.frame
    finish.set()
    assert instance.guest_refresh is not None
    await instance.guest_refresh
    assert transports[-1].sent[0]["payload"]["boot_epoch"] == epoch + 1
    assert instance.guest_tools is not None and (await instance.guest_tools.capabilities()).usable
