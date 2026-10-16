"""Exercise persistent disks, ownership and real IPC using supervised fake QEMU."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from pydantic import ValidationError
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from palette.agent_ipc import AgentCommand, RemoteService
from palette.config import PaletteConfig
from palette.desktop import DesktopManager
from palette.display import VERSION
from palette.errors import ErrorCode, PaletteError
from palette.operator import OperatorClient
from palette.operator.protocol import DesktopCommand
from palette.runtime import RuntimeOwner
from palette.service import PaletteService


@pytest.mark.parametrize("kind", ["daemon", "embedded"])
async def test_runtime_stop_requires_exited_processes_and_seals_starts(
    service: PaletteService, kind: str
) -> None:
    owner = RuntimeOwner(service, kind=kind)
    await owner.start()
    try:
        client = OperatorClient(service.config.runtime_root)
        assert "runtime-stop" in (await client.call("hello"))["features"]
        definition = await owner.desktop.create(draft(service))
        vm_id = definition["vm_id"]
        await owner.desktop.start(vm_id)
        instance = service.manager.get(vm_id)
        disk = instance.paths.overlay
        marker = disk.with_suffix(".userdata")
        marker.write_text("preserved")
        with pytest.raises(PaletteError) as active:
            await client.call("runtime.stop")
        assert active.value.code == ErrorCode.STATE_CONFLICT
        assert not owner.stopping.is_set()
        assert instance.process.returncode is None
        # State alone is insufficient: a FAILED VM can still own a live process.
        instance.view.state = "FAILED"
        with pytest.raises(PaletteError):
            await client.call("runtime.stop")
        instance.view.state = "RUNNING"
        with pytest.raises(ValidationError):
            AgentCommand.model_validate({"method": "runtime.stop", "parameters": {}})
        if kind == "embedded":
            with pytest.raises(PaletteError):
                await client.call("daemon.stop")
        await owner.desktop.stop(vm_id)
        async with service.manager._lock:
            stop = asyncio.create_task(owner.route(DesktopCommand(method="runtime.stop")))
            await asyncio.sleep(0)
            start = asyncio.create_task(service.manager.start(vm_id))
            await asyncio.sleep(0)
        assert await stop == {"stopping": True}
        with pytest.raises(PaletteError) as sealed:
            await start
        assert sealed.value.code == ErrorCode.STATE_CONFLICT
        with pytest.raises(PaletteError):
            await service.manager.reset(vm_id)
        await asyncio.wait_for(owner.stopping.wait(), 2)
    finally:
        await owner.close()
    assert disk.exists()
    assert marker.read_text() == "preserved"
    assert vm_id in DesktopManager(service).definitions


@pytest.fixture(autouse=True)
def desktop_shutdown_budget(config: PaletteConfig) -> None:
    profile = config.profiles["default"]
    config.profiles["default"] = profile.model_copy(
        update={"timeouts": profile.timeouts.model_copy(update={"shutdown_seconds": 2.0})}
    )


def draft(service: PaletteService, source: str = "image", path: str | None = None) -> dict:
    profile = service.config.profile("default")
    return {
        "name": "Workstation",
        "profile_id": "default",
        "source": source,
        "path": path or profile.images.base,
        "image_format": "qcow2",
        "vcpus": 1,
        "memory_mib": 512,
        "disk_gib": 24,
        "network": "offline",
    }


async def test_disk_survives_stop_resume_and_service_restart(service: PaletteService) -> None:
    desktop = DesktopManager(service)
    definition = await desktop.create(draft(service))
    vm_id = definition["vm_id"]
    await desktop.start(vm_id)
    first = service.manager.get(vm_id)
    original_disk = first.paths.overlay
    original_bytes = original_disk.read_bytes()
    marker = original_disk.with_suffix(".userdata")
    marker.write_text("saved")
    await desktop.stop(vm_id)
    await desktop.start(vm_id)
    second = service.manager.get(vm_id)
    assert second.view.instance_id != first.view.instance_id
    assert second.paths.overlay == original_disk
    assert original_disk.read_bytes() == original_bytes
    assert marker.read_text() == "saved"
    await desktop.stop(vm_id)
    rebuilt = PaletteService(service.config)
    await rebuilt.startup()
    try:
        registry = DesktopManager(rebuilt)
        await registry.start(vm_id)
        assert rebuilt.manager.get(vm_id).paths.overlay == original_disk
    finally:
        await rebuilt.shutdown()


async def test_lease_watcher_does_not_revoke_an_in_progress_takeover(
    service: PaletteService, monkeypatch
) -> None:
    await service.start("desktop")
    instance = service.manager.get("desktop")
    original = service.take_control

    async def delayed_takeover(vm_id: str, *, reason: str) -> int:
        epoch = await original(vm_id, reason=reason)
        # Cross the watcher's 250ms interval while acquire still owns its lock.
        await asyncio.sleep(0.35)
        return epoch

    monkeypatch.setattr(service, "take_control", delayed_takeover)
    lease = await service.control.acquire("desktop", instance.view.instance_id)
    await asyncio.sleep(0)
    assert service.control.heartbeat("desktop", lease["lease_id"])["expires_after_ms"] == 3000
    with pytest.raises(PaletteError, match="another operator"):
        await service.control.acquire("desktop", instance.view.instance_id)


async def test_operator_hold_excludes_agent_and_deduplicates(service: PaletteService) -> None:
    await service.start("desktop")
    instance = service.manager.get("desktop")
    lease = await service.control.acquire("desktop", instance.view.instance_id)
    with pytest.raises(PaletteError, match="another operator"):
        await service.control.acquire("desktop", instance.view.instance_id)
    args = {
        "vm_id": "desktop",
        "token": lease["lease_id"],
        "seq": 1,
        "display_epoch": instance.view.display_epoch,
        "width": 320,
        "height": 200,
        "events": [{"type": "key", "key": "a", "down": True}],
    }
    first = await service.control.input(**args)
    assert instance.controller.ledger_snapshot()["keys"] == ["a"]
    await service.observe_image("desktop")
    assert instance.controller.ledger_snapshot()["keys"] == ["a"]
    assert await service.control.input(**args) == first
    with pytest.raises(PaletteError, match="sequence has different input"):
        await service.control.input(
            **{**args, "events": [{"type": "key", "key": "b", "down": True}]}
        )
    with pytest.raises(PaletteError, match="display epoch changed"):
        await service.control.input(**{**args, "seq": 2, "display_epoch": -1})
    await service.control.input(
        **{
            **args,
            "seq": 2,
            "events": [
                {"type": "move", "x": 10, "y": 10},
                {"type": "button", "button": "left", "down": True},
            ],
        }
    )
    await service.control.input(
        **{**args, "seq": 3, "events": [{"type": "move", "x": 100, "y": 80}]}
    )
    assert instance.controller.ledger_snapshot()["buttons"] == ["left"]
    with pytest.raises(PaletteError, match="operator control"):
        await service.release_inputs("desktop", instance_id=instance.view.instance_id)
    observed, _ = await service.observe_image("desktop")
    with pytest.raises(PaletteError):
        await service.act(
            "desktop",
            instance_id=instance.view.instance_id,
            frame_id=observed.frame.frame_id,
            request_id="blocked",
            action={"type": "key", "key": "b"},
        )
    assert instance.controller.ledger_snapshot()["keys"] == ["a"]
    service.control.leases["desktop"].expires = time.monotonic() - 1
    await asyncio.sleep(0.4)
    assert instance.controller.ledger_snapshot()["keys"] == []
    assert "desktop" not in service.control.leases


async def test_cli_and_agent_reuse_registered_disk_and_delete(service: PaletteService) -> None:
    owner = RuntimeOwner(service, kind="daemon")
    await owner.start()
    try:
        vm_id = (await owner.desktop.create(draft(service)))["vm_id"]
        client = OperatorClient(service.config.runtime_root)
        await client.call("start", vm_id=vm_id)
        disk = service.manager.get(vm_id).paths.overlay
        await client.call("stop", vm_id=vm_id)
        remote = RemoteService(service.config, owner.agent.endpoint)
        await remote.start(vm_id)
        assert service.manager.get(vm_id).paths.overlay == disk
        with pytest.raises(PaletteError, match="does not grant agents"):
            await remote.stop(vm_id)
        await client.call("stop", vm_id=vm_id)
        await client.call("vms.delete", vm_id=vm_id)
        assert service.manager.find(vm_id) is None
        assert not disk.exists()
        assert not await owner.desktop.list()
    finally:
        await owner.close()


async def test_failed_restore_keeps_old_disk_under_retention(
    service: PaletteService, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = service.config.profiles["default"]
    service.config.profiles["default"] = profile.model_copy(
        update={"retention": profile.retention.model_copy(update={"keep_overlays": False})}
    )
    desktop = DesktopManager(service)
    vm_id = (await desktop.create(draft(service)))["vm_id"]
    await desktop.start(vm_id)
    disk = service.manager.get(vm_id).paths.overlay
    original = disk.read_bytes()
    await desktop.stop(vm_id)
    previous = desktop.get(vm_id).storage_owner

    async def fail_start(_: str) -> dict:
        service.manager._apply_retention(vm_id, desktop.get(vm_id).profile)
        raise PaletteError(ErrorCode.PRECONDITION_FAILED, "injected startup failure")

    monkeypatch.setattr(desktop, "_start", fail_start)
    with pytest.raises(PaletteError, match="injected startup failure"):
        await desktop.reset(vm_id)
    assert desktop.get(vm_id).storage_owner == previous
    assert disk.read_bytes() == original
    assert disk.parent.parent in service.manager.persistent_roots


async def test_display_ticket_is_single_use_and_disconnect_releases(
    service: PaletteService, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = RuntimeOwner(service, kind="daemon")
    await owner.start()
    revoked = asyncio.Event()
    original_revoke = service.control.revoke

    async def revoke(vm_id: str) -> bool:
        confirmed = await original_revoke(vm_id)
        revoked.set()
        return confirmed

    monkeypatch.setattr(service.control, "revoke", revoke)
    try:
        await service.start("desktop")
        instance = service.manager.get("desktop")
        url = owner.display.ticket("desktop", instance.view.instance_id)["url"]
        async with connect(url, origin="app://palette", proxy=None) as ws:
            assert await ws.recv() == VERSION
            for byte in VERSION:
                await ws.send(bytes([byte]))
            assert await ws.recv() == b"\x01\x01"
            await ws.send(b"\x01")
            assert await ws.recv() == b"\0" * 4
            await ws.send(b"\x01")
            assert len(await ws.recv()) >= 24
            lease = await service.control.acquire("desktop", instance.view.instance_id)
            async with connect(url, origin="app://palette", proxy=None) as reused:
                with pytest.raises(ConnectionClosed):
                    await reused.recv()
            assert service.control.validate("desktop", lease["lease_id"])
            assert not revoked.is_set()
            await ws.send(b"\x04\x01\0\0\0\0\0a")
            with pytest.raises(ConnectionClosed):
                await ws.recv()
            await asyncio.wait_for(revoked.wait(), timeout=2)
            assert "desktop" not in service.control.leases
    finally:
        await owner.close()


async def test_gui_graceful_stop_never_kills_on_failure(service: PaletteService) -> None:
    desktop = DesktopManager(service)
    definition = await desktop.create(draft(service))
    vm_id = definition["vm_id"]
    await desktop.start(vm_id)
    instance = service.manager.get(vm_id)
    Path(str(instance.paths.overlay) + ".behavior.json").write_text(
        json.dumps({"ignore_powerdown": True})
    )
    with pytest.raises(PaletteError):
        await desktop.stop(vm_id)
    assert instance.process.returncode is None
    assert instance.view.state == "RUNNING"
    await desktop.stop(vm_id, force=True)


async def test_guest_initiated_shutdown_cleans_up_and_preserves_the_persistent_disk(
    service: PaletteService,
) -> None:
    desktop = DesktopManager(service)
    definition = await desktop.create(draft(service))
    vm_id = definition["vm_id"]
    await desktop.start(vm_id)
    instance = service.manager.get(vm_id)
    disk = instance.paths.overlay
    before = disk.read_bytes()
    Path(str(disk) + ".behavior.json").write_text(json.dumps({"shutdown_on_input": True}))
    watcher = next(task for task in instance.tasks if task.get_name().startswith("watch-"))
    await instance.qmp.execute("input-send-event", {"events": []})
    await asyncio.wait_for(asyncio.shield(watcher), 5)
    assert instance.view.state == "STOPPED"
    assert instance.process.returncode == 0
    assert not instance.tasks
    assert not instance.qmp.connected
    assert instance.view.last_error is None
    assert not instance.view.qmp_ready
    assert not instance.view.screen_ready
    manifest = json.loads(instance.paths.manifest.read_text())
    assert manifest["state"] == "STOPPED" and manifest["process_exited"] is True
    assert disk.read_bytes() == before
    await desktop.start(vm_id)
    assert service.manager.get(vm_id).view.instance_id != instance.view.instance_id
    await desktop.stop(vm_id)


async def test_shutdown_confirmation_does_not_stop_a_replacement_instance(
    service: PaletteService,
) -> None:
    desktop = DesktopManager(service)
    definition = await desktop.create(draft(service))
    vm_id = definition["vm_id"]
    first = await desktop.start(vm_id)
    await desktop.stop(vm_id)
    second = await desktop.start(vm_id)
    assert second["instance_id"] != first["instance_id"]
    with pytest.raises(PaletteError) as error:
        await desktop.stop(vm_id, force=True, expected_instance_id=first["instance_id"])
    assert error.value.code is ErrorCode.STATE_CONFLICT
    assert service.manager.get(vm_id).view.state == "RUNNING"
    await desktop.stop(vm_id, expected_instance_id=second["instance_id"])


@pytest.mark.parametrize(
    ("guest", "reason", "exit_code"),
    [(False, "host-qmp-quit", 0), (True, "guest-panic", 0), (True, "guest-shutdown", 1)],
)
async def test_an_unclean_shutdown_is_still_reported_as_failed(
    service: PaletteService, guest: bool, reason: str, exit_code: int
) -> None:
    desktop = DesktopManager(service)
    definition = await desktop.create(draft(service))
    vm_id = definition["vm_id"]
    await desktop.start(vm_id)
    instance = service.manager.get(vm_id)
    Path(str(instance.paths.overlay) + ".behavior.json").write_text(
        json.dumps(
            {
                "shutdown_on_input": True,
                "shutdown_guest": guest,
                "shutdown_reason": reason,
                "shutdown_exit_code": exit_code,
            }
        )
    )
    watcher = next(task for task in instance.tasks if task.get_name().startswith("watch-"))
    await instance.qmp.execute("input-send-event", {"events": []})
    await asyncio.wait_for(asyncio.shield(watcher), 5)
    assert instance.view.state == "FAILED"
    assert instance.paths.overlay.exists()
    await desktop.stop(vm_id, force=True)


async def test_iso_blank_disk_and_install_media(service: PaletteService, tmp_path: Path) -> None:
    iso = tmp_path / "installer.iso"
    iso.write_bytes(b"installer")
    desktop = DesktopManager(service)
    definition = await desktop.create(draft(service, "iso", str(iso)))
    await desktop.start(definition["vm_id"])
    instance = service.manager.get(definition["vm_id"])
    assert any("scsi-cd" in value and "bootindex=0" in value for value in instance.plan.argv)
    assert any("virtio-blk-pci" in value and "bootindex=1" in value for value in instance.plan.argv)
    await desktop.stop(definition["vm_id"])
    current = await desktop.get_settings(definition["vm_id"])
    await desktop.update(
        definition["vm_id"],
        {
            "revision": current["revision"],
            "name": current["draft"]["name"],
            "vcpus": current["draft"]["vcpus"],
            "memory_mib": current["draft"]["memory_mib"],
            "network": current["draft"]["network"],
            "settings": current["settings"],
            "iso_attached": False,
        },
    )
    await desktop.start(definition["vm_id"])
    assert not service.manager.get(definition["vm_id"]).overrides.volumes


async def test_daemon_shares_ipc_and_binary_agent_image(service: PaletteService) -> None:
    owner = RuntimeOwner(service, kind="daemon")
    await owner.start()
    try:
        client = OperatorClient(service.config.runtime_root)
        info = await client.call("hello")
        assert info["owner_kind"] == "daemon"
        remote = RemoteService(service.config, owner.agent.endpoint)
        started = await remote.start("desktop")
        observed, png = await remote.observe_image("desktop")
        assert png.startswith(b"\x89PNG")
        assert observed.frame.instance_id == started.instance_id
        records = await client.call("vms.list")
        assert records["vms"][0]["status"]["instance_id"] == started.instance_id
        with pytest.raises(PaletteError) as denied:
            await remote._call("stop", "desktop", {"actor": "operator"})
        assert denied.value.code == ErrorCode.INVALID_ARGUMENT
    finally:
        await owner.close()
