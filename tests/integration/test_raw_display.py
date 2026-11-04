"""Raw display uses the same single-use tickets and cannot carry input."""

import asyncio
import json
import struct
from types import SimpleNamespace

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from palette.display import DisplayGateway
from palette.frame_source import FrameSource


@pytest.fixture
async def raw_display(service, running_vm):
    source = FrameSource(running_vm.view.instance_id)
    source.apply({"kind": "scanout", "width": 2, "height": 2}, b"\x77" * 16)
    running_vm.display_bridge = SimpleNamespace(source=source)
    gateway = DisplayGateway(service)
    await gateway.start()
    try:
        yield gateway, source, running_vm
    finally:
        source.close()
        await gateway.close()
        running_vm.display_bridge = None


async def test_raw_ticket_is_single_use_and_a_raw_client_cannot_mutate(raw_display, service):
    gateway, source, instance = raw_display
    ticket = gateway.ticket("desktop", instance.view.instance_id)
    assert ticket["provider"] == "raw"
    lease = await service.control.acquire("desktop", instance.view.instance_id)
    async with connect(ticket["url"], origin="app://palette") as ws:
        packet = await ws.recv()
        size = struct.unpack(">I", packet[:4])[0]
        header = json.loads(packet[4 : size + 4])
        assert header["generation"] == source.generation
        assert header["instance_id"] == instance.view.instance_id
        assert packet[size + 4 :] == b"\x77" * 16
        async with connect(ticket["url"], origin="app://palette") as duplicate:
            with pytest.raises(ConnectionClosed):
                await duplicate.recv()
        await ws.send(b"input")
        with pytest.raises(ConnectionClosed):
            await ws.recv()
    async with asyncio.timeout(2):
        while "desktop" in service.control.leases:  # noqa: ASYNC110 - bounded teardown observation
            await asyncio.sleep(0.01)
    assert lease["instance_id"] == instance.view.instance_id


async def test_old_source_ticket_does_not_connect_to_a_restarted_helper(raw_display, service):
    gateway, source, instance = raw_display
    ticket = gateway.ticket("desktop", instance.view.instance_id)
    replacement = FrameSource(instance.view.instance_id)
    instance.display_bridge = SimpleNamespace(source=replacement)
    lease = await service.control.acquire("desktop", instance.view.instance_id)
    async with connect(ticket["url"], origin="app://palette") as ws:
        with pytest.raises(ConnectionClosed):
            await ws.recv()
    assert service.control.leases["desktop"].token == lease["lease_id"]
    source.close()


async def test_helper_close_ends_the_view_and_next_ticket_uses_vnc(raw_display):
    gateway, source, instance = raw_display
    ticket = gateway.ticket("desktop", instance.view.instance_id)
    async with connect(ticket["url"], origin="app://palette") as ws:
        await ws.recv()
        source.close()
        with pytest.raises(ConnectionClosed):
            await ws.recv()
    instance.console_address = ("127.0.0.1", 5900)
    assert gateway.ticket("desktop", instance.view.instance_id)["provider"] == "rfb"


async def test_unpainted_frames_are_coalesced_until_exact_ack(raw_display):
    gateway, source, instance = raw_display
    ticket = gateway.ticket("desktop", instance.view.instance_id)
    async with connect(ticket["url"], origin="app://palette") as ws:
        initial = await ws.recv()
        size = struct.unpack(">I", initial[:4])[0]
        first = json.loads(initial[4 : size + 4])
        source.apply({"kind": "update", "x": 0, "y": 0, "width": 1, "height": 1}, b"\x11" * 4)
        source.apply({"kind": "update", "x": 1, "y": 1, "width": 1, "height": 1}, b"\x22" * 4)
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.05):
                await ws.recv()
        await ws.send(struct.pack(">Q", first["seq"]))
        latest = await ws.recv()
        size = struct.unpack(">I", latest[:4])[0]
        header = json.loads(latest[4 : size + 4])
        assert header["seq"] == first["seq"] + 2
        assert latest[size + 4 :] == b"\x11" * 4 + b"\x77" * 8 + b"\x22" * 4
