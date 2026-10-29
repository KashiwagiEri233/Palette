"""Upstream protocol behavior, partial availability, and private export boundaries."""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import struct
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import h11
import pytest

from palette.config import GuestDesktopConfig, QemuGuestAgentConfig, SharedFolderConfig
from palette.enhancements import enhancement_status
from palette.errors import ErrorCode, PaletteError
from palette.guest_agent import QemuGuestAgent
from palette.guest_desktop import CAPS, PROVIDE, TEXT, RfbGuestDesktop, decode_clipboard
from palette.guest_session import GuestSession
from palette.guest_shares import MUX, GuestShares
from palette.vm.launcher import build_launch_plan
from palette.vm.manager import VmState


class AgentWire:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes] = asyncio.Queue()
        self.sent: list[dict] = []
        self.ignore: set[str] = set()
        self.file = bytearray()
        self.connected = True
        self.metadata: dict = {}

    async def open(self):
        self.connected = True

    async def close(self):
        self.connected = False

    async def read(self, timeout=1):
        return await asyncio.wait_for(self.queue.get(), timeout)

    async def write(self, data):
        if data == b"\xff":
            return
        command = json.loads(data)
        self.sent.append(command)
        name = command["execute"]
        args = command.get("arguments", {})
        if name in self.ignore or name == "guest-shutdown":
            return
        result = {}
        if name == "guest-sync-delimited":
            result = args["id"]
        elif name == "guest-info":
            result = {
                "version": "test",
                "supported_commands": [
                    {"name": value, "enabled": True}
                    for value in [
                        "guest-exec",
                        "guest-set-time",
                        "guest-shutdown",
                        "guest-file-open",
                        "guest-file-read",
                        "guest-file-write",
                        "guest-file-close",
                        *self.metadata,
                    ]
                ],
            }
        elif name == "guest-file-open":
            result = 7
        elif name == "guest-file-write":
            payload = base64.b64decode(args["buf-b64"])
            self.file.extend(payload)
            result = {"count": len(payload)}
        elif name == "guest-file-read":
            result = {
                "count": len(self.file),
                "buf-b64": base64.b64encode(self.file).decode(),
                "eof": True,
            }
        elif name in self.metadata:
            result = self.metadata[name]
        response = (
            (b"\xff" if name == "guest-sync-delimited" else b"")
            + json.dumps({"return": result}).encode()
            + b"\n"
        )
        # Arbitrary transport fragmentation must not change the protocol.
        self.queue.put_nowait(response[:3])
        self.queue.put_nowait(response[3:])


def agent_for_test(max_file_bytes=16 << 20):
    agent = QemuGuestAgent("test-only", timeout_seconds=0.02, max_file_bytes=max_file_bytes)
    wire = AgentWire()
    agent.channel = wire
    return agent, wire


async def test_late_agent_is_detected_without_restarting_vm():
    agent, wire = agent_for_test()
    wire.ignore.add("guest-sync-delimited")
    assert not (await agent.refresh_info())["online"]
    wire.ignore.clear()
    assert (await agent.refresh_info())["online"]
    assert "guest-exec" not in agent.status()["commands"]
    await agent.sync_time()
    assert wire.sent[-1]["arguments"]["time"] > 0


async def test_unknown_mutation_is_not_replayed_and_next_connection_resynchronizes():
    agent, wire = agent_for_test()
    await agent.refresh_info()
    wire.ignore.add("guest-set-time")
    with pytest.raises(PaletteError) as error:
        await agent.sync_time()
    assert error.value.code == ErrorCode.ACTION_OUTCOME_UNKNOWN
    assert len([item for item in wire.sent if item["execute"] == "guest-set-time"]) == 1
    wire.queue.put_nowait(b'{"return":{}}\n')  # Late reply from the previous request.
    assert (await agent.refresh_info())["online"]
    assert len([item for item in wire.sent if item["execute"] == "guest-set-time"]) == 1


async def test_untrusted_guest_metadata_cannot_break_desktop_shapes():
    agent, wire = agent_for_test()
    wire.metadata = {
        "guest-get-host-name": {"host-name": ["wrong type"]},
        "guest-get-osinfo": {"pretty-name": "Linux", "kernel-release": {"wrong": "type"}},
        "guest-network-get-interfaces": ["invalid", {"name": "eth0", "ip-addresses": "invalid"}],
    }
    status = await agent.refresh_info()
    assert status["online"]
    assert status["hostname"] == {}
    assert status["os"] == {"pretty-name": "Linux"}
    assert status["interfaces"] == [{"name": "eth0", "ip-addresses": []}]


async def test_qga_files_have_opaque_boot_bound_handles_and_total_limit():
    agent, wire = agent_for_test(max_file_bytes=5)
    await agent.refresh_info()
    token = (await agent.file_open("/home/user/example", "w"))["transfer_id"]
    await agent.file_write(token, base64.b64encode(b"12345").decode())
    with pytest.raises(PaletteError):
        await agent.file_write(token, base64.b64encode(b"6").decode())
    assert wire.file == b"12345"
    await agent.close_files()
    assert not agent.transfers
    token = (await agent.file_open("/home/user/example", "r"))["transfer_id"]
    assert (await agent.file_read(token))["data_base64"] == base64.b64encode(b"12345").decode()
    agent.invalidate()
    with pytest.raises(PaletteError):
        await agent.file_read(token)


async def test_qga_control_revocation_fences_files_when_close_cannot_be_sent():
    agent, _ = agent_for_test()
    await agent.refresh_info()
    token = (await agent.file_open("/home/user/example", "w"))["transfer_id"]
    agent._synchronize = AsyncMock(side_effect=ConnectionError)
    await agent.close_files()
    assert token not in agent.transfers


async def test_shutdown_does_not_wait_for_nonexistent_success_response():
    agent, wire = agent_for_test()
    await agent.refresh_info()
    await asyncio.wait_for(agent.shutdown(), 0.05)
    assert wire.sent[-1]["execute"] == "guest-shutdown"


def test_unicode_clipboard_and_bounded_decompression():
    text = "中文\r\n🖼️"
    payload = text.encode() + b"\0"
    assert (
        decode_clipboard(zlib.compress(struct.pack(">I", len(payload)) + payload), 128) == "中文\n🖼️"
    )
    with pytest.raises(ValueError):
        decode_clipboard(zlib.compress(struct.pack(">I", 1 << 20) + b"x" * (1 << 20)), 128)


async def test_private_rfb_unicode_set_and_resize_forwarding(monkeypatch):
    reader = asyncio.StreamReader()
    writer = AsyncMock()
    writer.close = lambda: None
    writer.write = lambda data: sent.append(data)
    sent = []
    monkeypatch.setattr(asyncio, "open_connection", AsyncMock(return_value=(reader, writer)))
    name = b"test"
    header = struct.pack(">HH", 640, 480) + bytes(16) + struct.pack(">I", len(name))
    caps = struct.pack(">B3xiII", 3, -8, CAPS | PROVIDE | TEXT, 0)
    reader.feed_data(b"RFB 003.008\n\x01\x02" + bytes(16) + bytes(4) + header + name + caps)
    desktop = RfbGuestDesktop(("127.0.0.1", 1), "password")
    await desktop.connect()
    await desktop.clipboard_set("你好")
    payload = sent[-1]
    assert struct.unpack_from(">i", payload, 4)[0] < 0
    assert decode_clipboard(payload[12:], 128) == "你好"
    resize = asyncio.create_task(desktop.resize(1280, 800))
    await asyncio.sleep(0)
    reader.feed_data(
        struct.pack(">BBHHHHHi", 0, 0, 1, 1, 4, 640, 480, -308) + bytes([1, 0, 0, 0]) + bytes(16)
    )
    assert (await resize)["confirmed"] is False
    await desktop.close()


def dav_request(shares, method, target, body=b"", headers=None):
    request = h11.Request(
        method=method, target=target, headers=[(b"Host", b"localhost"), *(headers or [])]
    )
    return shares._response(request, body)


def test_webdav_readonly_unicode_listing_and_traversal(tmp_path):
    root = tmp_path / "export"
    root.mkdir()
    (root / "中文.txt").write_text("hello", encoding="utf-8")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    shares = GuestShares("test-only", [SharedFolderConfig(tag="shared", path=str(root))])
    assert dav_request(shares, b"GET", b"/shared/%E4%B8%AD%E6%96%87.txt")[2] == b"hello"
    assert dav_request(shares, b"PROPFIND", b"/shared/", headers=[(b"Depth", b"1")])[0] == 207
    assert dav_request(shares, b"PUT", b"/shared/no.txt", b"write")[0] == 403
    assert not (root / "no.txt").exists()
    assert dav_request(shares, b"GET", b"/shared/../secret.txt")[0] in {403, 404}
    assert dav_request(shares, b"GET", b"/shared/%2e%2e/secret.txt")[0] in {403, 404}


async def test_webdav_fragmented_multiplex_and_writable_export(tmp_path):
    shares = GuestShares(
        "test-only", [SharedFolderConfig(tag="shared", path=str(tmp_path), read_only=False)]
    )
    frames = []
    shares.channel.write = AsyncMock(side_effect=lambda data: frames.append(data))
    raw = b"PUT /shared/test.txt HTTP/1.1\r\nHost: localhost\r\nContent-Length: 4\r\n\r\ndata"
    for offset in range(0, len(raw), 7):
        await shares.feed(55, raw[offset : offset + 7])
    assert (tmp_path / "test.txt").read_bytes() == b"data"
    assert all(MUX.unpack_from(frame)[0] == 55 for frame in frames)
    assert b"201" in b"".join(frame[MUX.size :] for frame in frames)


def test_resize_helper_ignores_other_outputs_and_detects_changed_preferred():
    path = Path(__file__).resolve().parents[2] / "guest/linux/palette-auto-resize.py"
    spec = importlib.util.spec_from_file_location("resize_helper", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    modes = module.preferred_modes(
        "Virtual-1 connected 640x480\n   1280x800 60.00+\n   640x480 60.00*\nHDMI-1 connected\n   1920x1080 60.00*+\nVirtual-2 disconnected\n   800x600 60.00+"
    )
    assert modes == {"Virtual-1": ("1280x800", False)}


def test_launcher_uses_private_ports_and_can_omit_clipboard(profile, tmp_path):
    profile = profile.model_copy(
        update={
            "guest_agent": QemuGuestAgentConfig(enabled=True),
            "guest_desktop": GuestDesktopConfig(clipboard=True, resize=True, shared_folders=True),
        }
    )
    plan = build_launch_plan(
        profile=profile,
        binary="qemu",
        instance_id="instance",
        vm_id="desktop",
        overlay_path=tmp_path / "o.qcow2",
        workdir=tmp_path,
        managed_console=True,
        clipboard_available=False,
    )
    assert plan.guest_agent_endpoint and plan.session_endpoint and plan.webdav_endpoint
    assert not plan.clipboard_enabled
    assert not any("host=0.0.0.0" in arg for arg in plan.argv)
    assert not any("qemu-vdagent" in arg for arg in plan.argv)
    assert sum(arg.startswith("virtio-serial-pci") for arg in plan.argv) == 1


@pytest.mark.parametrize("model", ["virtio-gpu-pci", "virtio-vga"])
def test_features_degrade_independently_and_resize_requires_live_session(
    profile, monkeypatch, model
):
    session = GuestSession("test-only")
    profile = profile.model_copy(
        update={
            "devices": profile.devices.model_copy(update={"display_model": model}),
            "guest_desktop": GuestDesktopConfig(resize=True),
        }
    )
    instance = SimpleNamespace(
        state=VmState.RUNNING,
        qemu_guest_agent=None,
        guest_desktop=SimpleNamespace(connected=True),
        enhancement_ports={"palette-vdagent": True, "palette-session": True},
        plan=SimpleNamespace(clipboard_enabled=True),
        profile=profile,
        guest_session=session,
        guest_shares=None,
    )
    monkeypatch.setattr("palette.guest_session.time.monotonic", lambda: 10)
    assert enhancement_status(instance)["desktop"]["clipboard"]
    assert not enhancement_status(instance)["desktop"]["resize"]
    session.last_seen = 10
    assert enhancement_status(instance)["desktop"]["resize"]
    instance.enhancement_ports["palette-vdagent"] = False
    assert not enhancement_status(instance)["desktop"]["clipboard"]
    assert enhancement_status(instance)["desktop"]["resize"]
    monkeypatch.setattr("palette.guest_session.time.monotonic", lambda: 19)
    assert not enhancement_status(instance)["desktop"]["resize"]


@pytest.mark.parametrize("state", [state for state in VmState if state is not VmState.RUNNING])
def test_nonrunning_guest_does_not_advertise_live_enhancements(profile, state):
    agent = SimpleNamespace(status=lambda: {"online": True, "commands": ["guest-sync"]})
    shares = SimpleNamespace(status=lambda enabled: {"available": True})
    instance = SimpleNamespace(
        state=state,
        profile=profile,
        qemu_guest_agent=agent,
        guest_desktop=SimpleNamespace(connected=True),
        guest_shares=shares,
    )
    status = enhancement_status(instance)
    assert status["qga"]["online"] is False
    assert status["qga"]["commands"] == []
    assert status["desktop"]["clipboard"] is False
    assert status["desktop"]["resize"] is False
    assert status["shares"]["available"] is False


async def test_webdav_head_preserves_file_length_without_sending_body(tmp_path):
    (tmp_path / "file.txt").write_bytes(b"file content")
    shares = GuestShares("test-only", [SharedFolderConfig(tag="shared", path=str(tmp_path))])
    frames = []
    shares.channel.write = AsyncMock(side_effect=lambda data: frames.append(data))
    await shares.feed(1, b"HEAD /shared/file.txt HTTP/1.1\r\nHost: localhost\r\n\r\n")
    response = b"".join(frame[MUX.size :] for frame in frames)
    assert b"Content-Length: 12\r\n" in response
    assert response.endswith(b"\r\n\r\n")
