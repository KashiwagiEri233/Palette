"""Winsock attachment deadlines, endpoint ownership and the original QEMU race."""

from __future__ import annotations

import asyncio
import ctypes
import select
import socket
import sys
import tempfile
import uuid
from ctypes import wintypes
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from palette import windows_unix
from palette.errors import ErrorCode, PaletteError
from palette.guest_channel import GuestChannel


class FakeSocket:
    def __init__(self):
        self.closed = False

    def setblocking(self, value):
        pass

    def close(self):
        self.closed = True

    def getsockopt(self, *args):
        return 0


@pytest.mark.parametrize("pending", [False, True])
async def test_native_connect_retries_missing_endpoint_and_transfers_socket(monkeypatch, pending):
    connections = []

    def create(*args):
        result = FakeSocket()
        connections.append(result)
        return result

    errors = iter([10061, 10061, 10035 if pending else 0])
    pair = (object(), object())
    transfer = AsyncMock(return_value=pair)
    monkeypatch.setattr(windows_unix.socket, "socket", create)
    monkeypatch.setattr(windows_unix, "_connect", lambda *_: next(errors))
    monkeypatch.setattr(windows_unix.select, "select", lambda *args: ([], [connections[-1]], []))
    monkeypatch.setattr(asyncio, "open_connection", transfer)
    assert await windows_unix.open_connection("guest.sock") == pair
    assert [connection.closed for connection in connections] == [True, True, False]
    transfer.assert_awaited_once_with(sock=connections[-1], limit=65536)


async def test_native_permission_failure_is_not_retried(monkeypatch):
    connection = FakeSocket()
    monkeypatch.setattr(windows_unix.socket, "socket", lambda *_: connection)
    monkeypatch.setattr(windows_unix, "_connect", lambda *_: 10013)
    with pytest.raises(OSError) as failure:
        await windows_unix.open_connection("denied.sock")
    assert failure.value.errno == 10013
    assert connection.closed


async def test_cancel_pending_native_connect_reclaims_socket(monkeypatch):
    connection = FakeSocket()
    attempted = asyncio.Event()

    def connect(*args):
        attempted.set()
        return 10035

    monkeypatch.setattr(windows_unix.socket, "socket", lambda *_: connection)
    monkeypatch.setattr(windows_unix, "_connect", connect)
    monkeypatch.setattr(windows_unix.select, "select", lambda *_: ([], [], []))
    pending = asyncio.create_task(windows_unix.open_connection("pending.sock"))
    await attempted.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert connection.closed


async def test_cancel_during_stream_handoff_reclaims_socket(monkeypatch):
    connection = FakeSocket()
    monkeypatch.setattr(windows_unix.socket, "socket", lambda *_: connection)
    monkeypatch.setattr(windows_unix, "_connect", lambda *_: 0)
    monkeypatch.setattr(asyncio, "open_connection", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await windows_unix.open_connection("handoff.sock")
    assert connection.closed


def test_unsupported_host_reports_capability_failure(monkeypatch):
    def unsupported(*args):
        raise OSError(10047, "address family unsupported")

    monkeypatch.setattr(windows_unix.socket, "socket", unsupported)
    with pytest.raises(PaletteError) as error:
        windows_unix.require_support()
    assert error.value.code is ErrorCode.UNSUPPORTED_CAPABILITY


def test_endpoint_directory_checks_long_temp_path(monkeypatch, tmp_path):
    monkeypatch.setattr(windows_unix.tempfile, "gettempdir", lambda: str(tmp_path / ("x" * 100)))
    with pytest.raises(PaletteError) as error:
        windows_unix.endpoint_directory()
    assert error.value.code is ErrorCode.PRECONDITION_FAILED


def test_endpoint_limit_counts_encoded_bytes():
    assert len(windows_unix.validate_endpoint("a" * 107)) == 107
    for path in ("a" * 108, "中" * 36, "bad\0path"):
        with pytest.raises(PaletteError) as failure:
            windows_unix.validate_endpoint(path)
        assert failure.value.code is ErrorCode.PRECONDITION_FAILED


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows IPC")
def test_early_pipe_client_leaves_qemu_style_overlapped_accept_pending():
    from palette.operator.windows import _INVALID_HANDLE, _api, _get_last_error, _Overlapped

    api = _api()
    name = rf"\\.\pipe\palette-race-test-{uuid.uuid4().hex}"
    server = api.CreateNamedPipe(name, 0x40000003, 0, 1, 2048, 2048, 5000, None)
    assert server != _INVALID_HANDLE
    client = None
    event = api.CreateEvent(None, True, False, None)
    try:
        client = api.CreateFile(name, 0xC0000000, 0, None, 3, 0x40000000, None)
        assert client != _INVALID_HANDLE
        overlapped = _Overlapped(hEvent=event)
        assert not api.ConnectNamedPipe(server, ctypes.byref(overlapped))
        assert _get_last_error() == 535  # ERROR_PIPE_CONNECTED, omitted by QEMU.
        count = wintypes.DWORD()
        assert not api.GetOverlappedResult(
            server, ctypes.byref(overlapped), ctypes.byref(count), False
        )
        assert _get_last_error() == 996  # ERROR_IO_INCOMPLETE; bWait=TRUE would hang.
        assert overlapped.Internal == 259  # STATUS_PENDING despite a connected peer.
    finally:
        if client is not None and client != _INVALID_HANDLE:
            api.CloseHandle(client)
        api.CloseHandle(server)
        api.CloseHandle(event)


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows AF_UNIX")
async def test_windows_stream_io_timeout_reconnect_acl_and_cleanup():
    from palette.operator.windows import check_private

    with tempfile.TemporaryDirectory(prefix="pq-") as temporary:
        temporary_root = Path(temporary).resolve()
        assert temporary_root.parent == Path(tempfile.gettempdir()).resolve()
        root = temporary_root / "Palette" / "ipc" / "中文 空格"
        windows_unix.prepare_directory(root)
        check_private(root)
        endpoint = str(root / "qga.sock")
        address = windows_unix._Address(family=1, path=windows_unix.validate_endpoint(endpoint))
        library = ctypes.WinDLL("ws2_32")
        bind = library.bind
        bind.argtypes = [ctypes.c_size_t, ctypes.c_void_p, ctypes.c_int]
        bind.restype = ctypes.c_int
        accept = library.accept
        accept.argtypes = [ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]
        accept.restype = ctypes.c_size_t
        with socket.socket(1, socket.SOCK_STREAM) as listener:
            assert bind(listener.fileno(), ctypes.byref(address), ctypes.sizeof(address)) == 0
            listener.listen(2)

            def accept_peer():
                ready, _, _ = select.select([listener], [], [], 3)
                if not ready:
                    raise TimeoutError("test listener did not receive a connection")
                handle = accept(listener.fileno(), None, None)
                assert handle != ctypes.c_size_t(-1).value
                return socket.socket(1, socket.SOCK_STREAM, fileno=handle)

            channel = GuestChannel(endpoint)
            try:
                for _ in range(2):
                    await channel.open(timeout=2)
                    peer = await asyncio.to_thread(accept_peer)
                    with peer:
                        peer.settimeout(3)
                        with pytest.raises(TimeoutError):
                            await channel.read(timeout=0.01)
                        await channel.write(b"hello")
                        assert await asyncio.to_thread(peer.recv, 5) == b"hello"
                        peer.sendall(b"reply")
                        assert await channel.read(timeout=1) == b"reply"
                    await channel.close()
            finally:
                await channel.close()
        windows_unix.cleanup_directory(root, [endpoint])
        assert not root.exists()
