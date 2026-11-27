"""Authenticated settings alter future launches without replacing a running instance."""

import pytest
from pydantic import ValidationError

from palette.agent_ipc import AgentCommand
from palette.display_settings import DisplaySettings
from palette.operator import OperatorClient
from palette.runtime import RuntimeOwner


async def test_display_preference_survives_reload_and_requires_restart(service, monkeypatch):
    async def supported(*args):
        return True

    monkeypatch.setattr("palette.vm.manager.dbus_available", supported)
    owner = RuntimeOwner(service)
    await owner.start()
    try:
        client = OperatorClient(service.config.runtime_root)
        assert "display-settings-v1" in (await client.call("hello"))["features"]
        assert (await client.call("display.settings.get"))["configured_mode"] == "inherit"
        assert not (service.config.runtime_root / "display-settings.json").exists()
        await client.call("display.settings.update", payload={"mode": "low_latency"})
        assert DisplaySettings(service.config.runtime_root).mode == "low_latency"
        instance = await service.manager.start("desktop")
        assert instance.plan.steady_display
        assert not instance.plan.experimental_display
        assert instance.display_bridge is None
        assert instance.plan.argv[instance.plan.argv.index("-display") + 1] == "dbus,p2p=yes,gl=off"
        process = instance.process
        result = await client.call("display.settings.update", payload={"mode": "compatibility"})
        assert result["instances"][0]["restart_required"]
        assert result["instances"][0]["refresh_mode"] == "steady"
        assert result["instances"][0]["provider"] == "rfb"
        assert instance.process is process and process.returncode is None
        await client.call(
            "vms.reboot",
            vm_id="desktop",
            payload={"expected_instance_id": instance.view.instance_id},
        )
        assert instance.process is process and instance.plan.steady_display
        assert (await client.call("display.settings.get"))["instances"][0]["restart_required"]
        await service.manager.stop("desktop", force=True)
        replacement = await service.manager.start("desktop")
        assert not replacement.plan.steady_display
        assert replacement.plan.argv[replacement.plan.argv.index("-display") + 1] == "none"
        assert not (await client.call("display.settings.get"))["instances"][0]["restart_required"]
        await service.manager.stop("desktop", force=True)
    finally:
        await owner.close()
    restored = RuntimeOwner(type(service)(service.config))
    assert restored.service.manager.display_settings.mode == "compatibility"
    assert not list(service.config.runtime_root.glob("display-settings-*.tmp"))


async def test_unsupported_qemu_reports_the_actual_compatible_mode(service, monkeypatch):
    async def unavailable(*args):
        return False

    monkeypatch.setattr("palette.vm.manager.dbus_available", unavailable)
    service.manager.console_enabled = True
    service.manager.display_settings.update({"mode": "low_latency"})
    instance = await service.manager.start("desktop")
    try:
        assert not instance.plan.steady_display
        assert instance.display_reason
        assert instance.display_requested_mode == "low_latency"
        state = RuntimeOwner(service)._display_settings()
        assert state["instances"][0]["refresh_mode"] == "adaptive"
        assert not state["instances"][0]["restart_required"]
        assert state["instances"][0]["reason"]
    finally:
        await service.manager.stop("desktop", force=True)


async def test_explicit_compatibility_overrides_the_raw_experiment(service, monkeypatch):
    async def must_not_probe(*args):
        raise AssertionError("compatibility cannot activate D-Bus or a helper")

    monkeypatch.setattr("palette.vm.manager.display_available", must_not_probe)
    service.manager.console_enabled = True
    monkeypatch.setattr(
        service.manager,
        "config",
        service.config.model_copy(
            update={"host": service.config.host.model_copy(update={"experimental_display": "dbus"})}
        ),
    )
    service.manager.display_settings.update({"mode": "compatibility"})
    instance = await service.manager.start("desktop")
    try:
        assert not instance.plan.experimental_display and not instance.plan.steady_display
        assert instance.display_bridge is None
    finally:
        await service.manager.stop("desktop", force=True)


def test_agent_channel_cannot_change_operator_display_settings():
    with pytest.raises(ValidationError):
        AgentCommand.model_validate(
            {"method": "display.settings.update", "parameters": {"mode": "low_latency"}}
        )
