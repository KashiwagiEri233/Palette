"""QMP readiness precedes optional services, with phase-specific startup errors."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from palette.errors import ErrorCode, PaletteError
from palette.guest_channel import GuestChannel
from palette.qmp.client import QmpClient


def enable_guest_services(profile):
    return profile.model_copy(
        update={
            "guest_agent": profile.guest_agent.model_copy(update={"enabled": True}),
            "guest_tools": profile.guest_tools.model_copy(update={"enabled": True}),
            "guest_desktop": profile.guest_desktop.model_copy(
                update={"resize": True, "shared_folders": True}
            ),
        }
    )


async def test_guest_connectors_only_run_after_management_is_ready(service, profile, monkeypatch):
    seen = []
    all_connected = asyncio.Event()

    async def open_after_cont(channel, **kwargs):
        instance = service.manager.get("desktop")
        assert instance.view.qmp_ready
        assert instance.qmp.connected
        assert instance.view.state == "RUNNING"
        seen.append(channel.endpoint)
        if len(seen) == 4:
            all_connected.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(GuestChannel, "open", open_after_cont)
    instance = await service.manager.start(
        "desktop", profile_override=enable_guest_services(profile)
    )
    await asyncio.wait_for(all_connected.wait(), 2)
    assert instance.view.screen_ready
    capabilities = await instance.guest_tools.capabilities()
    assert capabilities.enabled and not capabilities.usable
    assert "connecting" in capabilities.detail
    report = service.manager._guest_channel_report(instance)
    assert set(report) == {"qga", "tools", "shares", "session"}
    assert all(not channel["connected"] for channel in report.values())
    directory = instance.plan.guest_ipc_directory
    await service.manager.stop("desktop", force=True)
    assert directory is None or not directory.exists()


async def test_negotiation_failure_reports_all_enabled_channels(service, profile, monkeypatch):
    failure = PaletteError(ErrorCode.QMP_UNAVAILABLE, "test greeting failure")
    monkeypatch.setattr(QmpClient, "start", AsyncMock(side_effect=failure))
    with pytest.raises(PaletteError) as error:
        await service.manager.start("desktop", profile_override=enable_guest_services(profile))
    details = error.value.details
    assert error.value.code is ErrorCode.STARTUP_FAILED
    assert set(details["guest_channels"]) == {"qga", "tools", "shares", "session"}
    assert details["guest_pipes"] == dict.fromkeys(("qga", "tools", "shares", "session"), False)
    assert "pipe chardev" not in details["hint"]
    assert "qemu_pid" in details and "returncode" in details
    assert details["qmp"]["greeting_received"] is False
