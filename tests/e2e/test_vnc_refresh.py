"""Real QEMU refresh mechanism only; a paused diskless guest is not desktop acceptance."""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import struct
import subprocess
import time
from pathlib import Path

import pytest

from palette.vm.launcher import CREATION_FLAGS, build_child_env

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not os.environ.get("PALETTE_REFRESH_QEMU"),
        reason="set PALETTE_REFRESH_QEMU for isolated real QEMU refresh validation",
    ),
]


def _read(sock: socket.socket, count: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < count:
        value = sock.recv(count - len(chunks))
        if not value:
            raise EOFError("QEMU closed the isolated test connection")
        chunks.extend(value)
    return bytes(chunks)


def _trace(qemu: str, display: str) -> list[int]:
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    assert port >= 5900
    process = subprocess.Popen(  # noqa: S603 - explicit operator-selected test executable
        [
            qemu,
            "-S",
            "-nodefaults",
            "-machine",
            "q35,accel=tcg",
            "-m",
            "128",
            "-device",
            "VGA",
            "-display",
            display,
            "-vnc",
            f"127.0.0.1:{port - 5900}",
            "-trace",
            "enable=console_refresh",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=build_child_env(),
        creationflags=CREATION_FLAGS,
    )
    connection = None
    try:
        deadline = time.monotonic() + 5
        while connection is None:
            try:
                connection = socket.create_connection(("127.0.0.1", port), timeout=1)
            except OSError:
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
        _read(connection, 12)
        connection.sendall(b"RFB 003.008\n")
        assert 1 in _read(connection, _read(connection, 1)[0])
        connection.sendall(b"\x01")
        assert _read(connection, 4) == b"\0" * 4
        connection.sendall(b"\x01")
        header = _read(connection, 24)
        _read(connection, struct.unpack("!I", header[20:24])[0])
        time.sleep(6)
    finally:
        if connection is not None:
            connection.close()
        process.kill()
        _, stderr = process.communicate(timeout=5)
    return [int(value) for value in re.findall(rb"console_refresh interval (\d+) ms", stderr)]


async def test_dbus_without_helper_keeps_vnc_refresh_steady(tmp_path, record_property):
    qemu = str(Path(os.environ["PALETTE_REFRESH_QEMU"]).resolve())
    adaptive = await asyncio.to_thread(_trace, qemu, "none")
    steady = await asyncio.to_thread(_trace, qemu, "dbus,p2p=yes,gl=off")
    assert any(300 < value < 3000 for value in adaptive), adaptive
    assert steady and set(steady) == {30}, steady
    report = {
        "kind": "refresh-mechanism-only",
        "qemu": qemu,
        "guest": "paused, diskless, no guest input",
        "adaptive_trace_ms": adaptive,
        "steady_trace_ms": steady,
        "helper_started": False,
        "desktop_acceptance": False,
    }
    (tmp_path / "refresh-mechanism.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    record_property("refresh_mechanism", report)
