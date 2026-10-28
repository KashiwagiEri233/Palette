"""Lease-bound input streams over real local transports and supervised fake QEMU."""

from __future__ import annotations

import asyncio
import contextlib
import uuid

import pytest

from palette.operator.client import OperatorClient
from palette.operator.paths import IS_WINDOWS
from palette.operator.protocol import Request, decode_json, receive_frame, send_frame
from palette.operator.unix import UnixChannel
from palette.runtime import RuntimeOwner
from palette.service import PaletteService


async def channel_for(owner):
    if IS_WINDOWS:
        from palette.operator.windows import connect

        return await connect(owner.operator.endpoint.address)
    return UnixChannel(*(await asyncio.open_unix_connection(owner.operator.endpoint.address)))


def request(owner, method, vm_id="desktop", **payload):
    return Request(
        generation=owner.operator.endpoint.generation,
        token=owner.operator.endpoint.token,
        request_id=uuid.uuid4().hex,
        command={"method": method, "vm_id": vm_id, "payload": payload},
    )


async def exchange(channel, message):
    await send_frame(channel, message)
    return decode_json(await receive_frame(channel))


@pytest.fixture
async def stream_fixture(service: PaletteService):
    owner = RuntimeOwner(service)
    await owner.start()
    try:
        await service.start("desktop")
        instance = service.manager.get("desktop")
        lease = await service.control.acquire("desktop", instance.view.instance_id)
        channel = await channel_for(owner)
        try:
            opened = await exchange(
                channel, request(owner, "control.stream.open", lease_id=lease["lease_id"])
            )
            assert opened["ok"]
            yield owner, channel, lease, instance
        finally:
            await channel.close()
    finally:
        await owner.close()


def input_request(owner, lease, instance, seq, events=None):
    return request(
        owner,
        "control.input",
        lease_id=lease["lease_id"],
        seq=seq,
        display_epoch=instance.view.display_epoch,
        width=instance.view.native_size.width,
        height=instance.view.native_size.height,
        events=events or [{"type": "move", "x": seq, "y": seq}],
    )


async def test_multiple_inputs_and_receipts_share_one_connection(stream_fixture) -> None:
    owner, channel, lease, instance = stream_fixture
    for seq in range(1, 11):
        message = input_request(owner, lease, instance, seq)
        response = await exchange(channel, message)
        assert response["ok"]
        assert response["result"]["seq"] == seq
    assert len(owner.operator._input_streams) == 1
    # A repeated sequence with the same input has no second effect, even with a new request id.
    duplicate = await exchange(channel, input_request(owner, lease, instance, 10))
    assert duplicate["result"] == response["result"]
    client = OperatorClient(owner.service.config.runtime_root)
    metrics = await client.call("control.metrics", vm_id="desktop")
    assert metrics["input"]["qmp_ms"]["count"] == 10
    assert "operator-input-stream-v1" in (await client.call("hello"))["features"]


async def test_repeated_press_release_press_in_one_batch_is_released_on_takeover_end(
    stream_fixture,
):
    owner, channel, lease, instance = stream_fixture
    events = [
        {"type": kind, **identity, "down": down}
        for kind, identity in [("key", {"key": "a"}), ("button", {"button": "left"})]
        for down in (True, False, True)
    ]
    assert (await exchange(channel, input_request(owner, lease, instance, 1, events)))["ok"]
    assert instance.controller.ledger_snapshot()["keys"] == ["a"]
    assert instance.controller.ledger_snapshot()["buttons"] == ["left"]
    released = await owner.service.control.release("desktop", lease["lease_id"])
    assert released["release_confirmed"]
    assert instance.controller.ledger_snapshot()["keys"] == []
    assert instance.controller.ledger_snapshot()["buttons"] == []


async def test_idle_stream_survives_frame_deadline_with_independent_heartbeats(stream_fixture):
    owner, channel, lease, instance = stream_fixture
    client = OperatorClient(owner.service.config.runtime_root)
    for _ in range(4):
        await asyncio.sleep(0.6)
        await client.call(
            "control.heartbeat", vm_id="desktop", payload={"lease_id": lease["lease_id"]}
        )
    assert (await exchange(channel, input_request(owner, lease, instance, 1)))["ok"]


async def test_stream_rejects_rebinding_and_releases_held_keys(stream_fixture):
    owner, channel, lease, instance = stream_fixture
    message = input_request(owner, lease, instance, 1, [{"type": "key", "key": "a", "down": True}])
    assert (await exchange(channel, message))["ok"]
    changed = input_request(owner, lease, instance, 2)
    changed.command.vm_id = "another-vm"
    await send_frame(channel, changed)
    with pytest.raises((EOFError, OSError, asyncio.IncompleteReadError)):
        await receive_frame(channel)
    for _ in range(300):
        if "desktop" not in owner.service.control.leases:
            break
        await asyncio.sleep(0.01)
    assert "desktop" not in owner.service.control.leases
    assert instance.controller.ledger_snapshot()["keys"] == []


async def test_capacity_rejection_does_not_revoke_existing_lease(stream_fixture):
    owner, _channel, lease, _instance = stream_fixture
    duplicate = await channel_for(owner)
    try:
        response = await exchange(
            duplicate, request(owner, "control.stream.open", lease_id=lease["lease_id"])
        )
        assert response["error"]["code"] == "BUSY"
        owner.service.control.validate("desktop", lease["lease_id"])
    finally:
        await duplicate.close()


async def test_heartbeat_and_release_are_not_blocked_by_inflight_input(stream_fixture, monkeypatch):
    owner, channel, lease, instance = stream_fixture
    entered = asyncio.Event()
    original = instance.controller._send_events

    async def blocked(events, **kwargs):
        if any(event.get("type") == "key" and event["data"]["down"] for event in events):
            entered.set()
            await asyncio.Event().wait()
        await original(events, **kwargs)

    monkeypatch.setattr(instance.controller, "_send_events", blocked)
    message = input_request(owner, lease, instance, 1, [{"type": "key", "key": "a", "down": True}])
    await send_frame(channel, message)
    await asyncio.wait_for(entered.wait(), 3)
    client = OperatorClient(owner.service.config.runtime_root)
    async with asyncio.timeout(3):
        await client.call(
            "control.heartbeat", vm_id="desktop", payload={"lease_id": lease["lease_id"]}
        )
        released = await client.call(
            "control.release", vm_id="desktop", payload={"lease_id": lease["lease_id"]}
        )
    assert released["release_confirmed"]
    assert instance.controller.ledger_snapshot()["keys"] == []
    with contextlib.suppress(EOFError, OSError, asyncio.IncompleteReadError):
        response = decode_json(await receive_frame(channel))
        assert not response["ok"]


async def test_expiry_closes_idle_stream_and_releases_buttons(stream_fixture):
    owner, channel, lease, instance = stream_fixture
    assert (
        await exchange(
            channel,
            input_request(
                owner, lease, instance, 1, [{"type": "button", "button": "left", "down": True}]
            ),
        )
    )["ok"]
    owner.service.control.leases["desktop"].expires = 0
    with pytest.raises((EOFError, OSError, asyncio.IncompleteReadError)):
        await receive_frame(channel, timeout=3)
    for _ in range(300):
        if "desktop" not in owner.service.control.leases:
            break
        await asyncio.sleep(0.01)
    assert "desktop" not in owner.service.control.leases
    assert instance.controller.ledger_snapshot()["buttons"] == []


async def test_started_but_incomplete_frame_times_out_and_releases_input(stream_fixture):
    owner, channel, lease, instance = stream_fixture
    assert (
        await exchange(
            channel,
            input_request(owner, lease, instance, 1, [{"type": "key", "key": "a", "down": True}]),
        )
    )["ok"]
    await channel.write(b"\0")
    with pytest.raises((EOFError, OSError, asyncio.IncompleteReadError)):
        await receive_frame(channel, timeout=3)
    for _ in range(300):
        if "desktop" not in owner.service.control.leases:
            break
        await asyncio.sleep(0.01)
    assert "desktop" not in owner.service.control.leases
    assert instance.controller.ledger_snapshot()["keys"] == []
