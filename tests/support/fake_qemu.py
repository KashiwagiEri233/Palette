#!/usr/bin/env python3
"""Test double for ``qemu-system-x86_64``.

This is not a mock object inside the test process: it is a real child process started by
the real launcher, over real pipes, speaking the real QMP framing. That is the point —
the lifecycle, handshake, capture, input and teardown paths in ``palette`` are exercised
exactly as they would be against QEMU, without requiring a hypervisor or a guest image.

It answers the command-line probes too (``--version``, ``-accel help``, ``-device help``),
so a profile pointed at it passes ``palette doctor`` and the version/accelerator checks
meaningfully rather than vacuously.

Behaviour that a test wants to vary after the VM has started (for example "this build
cannot write PNG screendumps") is read lazily from ``<overlay>.behavior.json``, which the
test writes next to the overlay file.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import struct
import sys
import threading
import time
import zlib

VERSION = "11.1.2"
#: Small by design: the code paths for a 320x200 surface are the same as for
#: 1280x800, and rendering a million pixels in pure Python is not worth the wait.
#: A test that needs a different size writes a behavior file next to the overlay.
DEFAULT_WIDTH = 320
DEFAULT_HEIGHT = 200

DEVICE_HELP = """\
Installed devices:
name "VGA", bus PCI, desc "Standard VGA"
name "qemu-xhci", bus PCI, desc "USB eXtensible Host Controller"
name "usb-kbd", bus USB, desc "Standard USB Keyboard"
name "usb-tablet", bus USB, desc "USB Tablet"
name "virtio-blk-pci", bus PCI, desc "Virtio-blk PCI"
name "virtio-serial-pci", bus PCI, desc "Virtio Serial"
"""

ACCEL_HELP = """\
Accelerators supported in QEMU binary:
tcg
whpx
hvf
kvm
"""

COMMANDS = [
    "qmp_capabilities",
    "query-version",
    "query-commands",
    "query-qmp-schema",
    "query-status",
    "query-mice",
    "query-vnc",
    "set_password",
    "query-cpus-fast",
    "query-block",
    "qom-list",
    "input-send-event",
    "screendump",
    "cont",
    "stop",
    "system_powerdown",
    "system_reset",
    "quit",
]

SCHEMA = [
    {"name": "qmp_capabilities", "meta-type": "command"},
    {"name": "query-version", "meta-type": "command"},
    {"name": "query-commands", "meta-type": "command"},
    {
        "name": "screendump",
        "meta-type": "command",
        "arg-type": [
            {"name": "filename", "type": "str"},
            {"name": "device", "type": "str"},
            {"name": "head", "type": "int"},
            {"name": "format", "type": "ImageFormat"},
        ],
    },
    {"name": "input-send-event", "meta-type": "command"},
]


class FakeQemu:
    def __init__(self, argv: list[str]) -> None:
        self.argv = argv
        self.paused = "-S" in argv
        self.overlay_path = self._find_overlay_path()
        self.log_path = f"{self.overlay_path}.log.jsonl" if self.overlay_path else None
        self.behavior_path = f"{self.overlay_path}.behavior.json" if self.overlay_path else None
        self.events: list[str] = []
        self.vnc_password = ""
        self.vnc_port: int | None = None
        if "-vnc" in argv:
            self.start_vnc()

    def start_vnc(self) -> None:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        self.vnc_port = listener.getsockname()[1]

        def client(connection: socket.socket) -> None:
            def read(size: int) -> bytes:
                value = b""
                while len(value) < size:
                    chunk = connection.recv(size - len(value))
                    if not chunk:
                        raise EOFError
                    value += chunk
                return value

            try:
                from Crypto.Cipher import DES

                connection.settimeout(10)
                connection.sendall(b"RFB 003.008\n")
                read(12)
                connection.sendall(b"\x01\x02")
                if read(1) != b"\x02":
                    return
                challenge = bytes(range(16))
                connection.sendall(challenge)
                key = bytes(
                    int(f"{byte:08b}"[::-1], 2)
                    for byte in self.vnc_password.encode().ljust(8, b"\0")[:8]
                )
                if read(16) != DES.new(key, DES.MODE_ECB).encrypt(challenge):  # noqa: S304 - test VNCAuth
                    return
                connection.sendall(b"\0" * 4)
                read(1)
                name = b"Fake QEMU"
                connection.sendall(
                    struct.pack(
                        ">HHBBBBHHHBBBxxxI",
                        DEFAULT_WIDTH,
                        DEFAULT_HEIGHT,
                        32,
                        24,
                        0,
                        1,
                        255,
                        255,
                        255,
                        16,
                        8,
                        0,
                        len(name),
                    )
                    + name
                )
                while True:
                    kind = read(1)[0]
                    if kind == 0:
                        read(19)
                    elif kind == 2:
                        header = read(3)
                        read(struct.unpack(">H", header[1:])[0] * 4)
                    elif kind == 3:
                        read(9)
                        time.sleep(0.03)
                        connection.sendall(
                            b"\0\0\0\x01"
                            + struct.pack(">HHHHi", 0, 0, DEFAULT_WIDTH, DEFAULT_HEIGHT, 0)
                            + bytes([40, 100, 170, 0]) * DEFAULT_WIDTH * DEFAULT_HEIGHT
                        )
                    else:
                        raise ValueError("unexpected RFB message")
            except (OSError, EOFError, ValueError):
                pass
            finally:
                connection.close()

        def accept() -> None:
            while True:
                connection, _ = listener.accept()
                threading.Thread(target=client, args=(connection,), daemon=True).start()

        threading.Thread(target=accept, daemon=True).start()

    # -- configuration ---------------------------------------------------------------
    def _find_overlay_path(self) -> str | None:
        for index, arg in enumerate(self.argv):
            if arg == "-blockdev" and index + 1 < len(self.argv):
                try:
                    node = json.loads(self.argv[index + 1])
                except json.JSONDecodeError:
                    continue
                filename = node.get("filename")
                if filename:
                    return str(filename)
        return None

    def behavior(self) -> dict:
        if not self.behavior_path or not os.path.isfile(self.behavior_path):
            return {}
        try:
            with open(self.behavior_path, encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {}

    def log(self, record: dict) -> None:
        if not self.log_path:
            return
        try:
            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, separators=(",", ":")) + "\n")
        except OSError:
            pass

    # -- framing ---------------------------------------------------------------------
    def send(self, payload: dict) -> None:
        sys.stdout.write(json.dumps(payload, separators=(",", ":")))
        sys.stdout.write("\n")
        sys.stdout.flush()

    def send_event(self, name: str, data: dict | None = None) -> None:
        message = {"event": name, "timestamp": {"seconds": int(time.time()), "microseconds": 0}}
        if data is not None:
            message["data"] = data
        self.send(message)

    # -- commands --------------------------------------------------------------------
    def handle(self, message: dict) -> None:
        command = message.get("execute")
        arguments = message.get("arguments") or {}
        message_id = message.get("id")
        self.log({"command": command, "arguments": arguments, "id": message_id})

        if command == "qmp_capabilities":
            self.reply(message_id, {})
        elif command == "query-version":
            self.reply(
                message_id,
                {
                    "qemu": {"major": 11, "minor": 1, "micro": 2},
                    "package": f"v{VERSION}-fake",
                },
            )
        elif command == "query-commands":
            self.reply(message_id, [{"name": name} for name in COMMANDS])
        elif command == "query-qmp-schema":
            self.reply(message_id, SCHEMA)
        elif command == "set_password":
            self.vnc_password = arguments["password"]
            self.reply(message_id, {})
        elif command == "query-vnc":
            self.reply(
                message_id,
                {
                    "enabled": self.vnc_port is not None,
                    "host": "127.0.0.1",
                    "service": str(self.vnc_port),
                },
            )
        elif command == "query-status":
            self.reply(
                message_id,
                {"status": "paused" if self.paused else "running", "running": not self.paused},
            )
        elif command == "query-mice":
            self.reply(
                message_id,
                [
                    {
                        "name": "agent-pointer",
                        "index": 3,
                        "current": True,
                        "absolute": True,
                    }
                ],
            )
        elif command == "query-cpus-fast":
            self.reply(message_id, [{"cpu-index": 0, "target": "x86_64", "thread-id": os.getpid()}])
        elif command == "query-block" or command == "qom-list":
            self.reply(message_id, [])
        elif command == "input-send-event":
            self.reply(message_id, {})
        elif command == "screendump":
            self.screendump(message_id, arguments)
        elif command == "cont":
            self.paused = False
            self.reply(message_id, {})
            self.send_event("RESUME")
        elif command == "stop":
            self.paused = True
            self.reply(message_id, {})
            self.send_event("STOP")
        elif command == "system_reset":
            self.reply(message_id, {})
            self.send_event("RESET", {"guest": False, "reason": "host-qmp-system-reset"})
        elif command == "system_powerdown":
            self.reply(message_id, {})
            self.send_event("POWERDOWN")
            if self.behavior().get("ignore_powerdown"):
                # A guest that does not shut down: the manager must fall through to the
                # forced path rather than waiting forever or claiming success.
                return
            threading.Timer(0.15, lambda: os._exit(0)).start()
        elif command == "quit":
            self.reply(message_id, {})
            sys.stdout.flush()
            os._exit(0)
        else:
            self.send(
                {
                    "error": {
                        "class": "CommandNotFound",
                        "desc": f"The command {command} has not been found",
                    },
                    "id": message_id,
                }
            )

    def reply(self, message_id: str | None, result: object) -> None:
        self.send({"return": result, "id": message_id})

    # -- screendump ------------------------------------------------------------------
    def screendump(self, message_id: str | None, arguments: dict) -> None:
        behavior = self.behavior()
        fmt = arguments.get("format")
        filename = arguments.get("filename")
        device = arguments.get("device")

        if device is not None and behavior.get("unknown_device") == device:
            self.send(
                {
                    "error": {"class": "DeviceNotFound", "desc": f"Device {device!r} not found"},
                    "id": message_id,
                }
            )
            return
        if fmt == "png" and behavior.get("png_unsupported"):
            # The shape QEMU produces when the build has no libpng support.
            self.send(
                {
                    "error": {
                        "class": "GenericError",
                        "desc": "screendump: Parameter 'format' expects "
                        "'ImageFormat' (json) but got a value that is not supported",
                    },
                    "id": message_id,
                }
            )
            return
        if not filename:
            self.send(
                {
                    "error": {"class": "GenericError", "desc": "filename is required"},
                    "id": message_id,
                }
            )
            return
        if behavior.get("screendump_fails"):
            self.send(
                {
                    "error": {"class": "GenericError", "desc": "could not open file"},
                    "id": message_id,
                }
            )
            return

        width = int(behavior.get("width", DEFAULT_WIDTH))
        height = int(behavior.get("height", DEFAULT_HEIGHT))
        try:
            os.makedirs(os.path.dirname(filename), exist_ok=True)
            if fmt == "png" or filename.lower().endswith(".png"):
                data = render_png(width, height, frame=int(time.time() * 4) % 16)
            else:
                data = render_ppm(width, height, frame=0)
            with open(filename, "wb") as handle:
                handle.write(data)
        except OSError as exc:
            self.send(
                {
                    "error": {"class": "GenericError", "desc": f"could not write file: {exc}"},
                    "id": message_id,
                }
            )
            return
        self.reply(message_id, {})


# ------------------------------------------------------------------------------------
# image rendering
# ------------------------------------------------------------------------------------


def _pixels(width: int, height: int, *, frame: int) -> bytes:
    """A deterministic grid: recognisable, cheap, and stable between frames."""
    rows = []
    step_x = max(1, width // 16)
    step_y = max(1, height // 10)
    for y in range(height):
        row = bytearray()
        band = (y // step_y) % 2
        for x in range(width):
            if x % step_x == 0 or y % step_y == 0:
                row += bytes((32, 32, 32))
            else:
                shade = 128 + (64 if band else 0) + (frame % 4)
                row += bytes((shade, shade, 200))
        rows.append(bytes(row))
    return b"".join(rows)


def render_ppm(width: int, height: int, *, frame: int) -> bytes:
    return f"P6\n{width} {height}\n255\n".encode("ascii") + _pixels(width, height, frame=frame)


def render_png(width: int, height: int, *, frame: int) -> bytes:
    raw = _pixels(width, height, frame=frame)
    stride = width * 3
    scanlines = b"".join(b"\x00" + raw[y * stride : (y + 1) * stride] for y in range(height))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(scanlines, 6))
        + chunk(b"IEND", b"")
    )


def main(argv: list[str]) -> int:
    if "--version" in argv:
        print(f"QEMU emulator version {VERSION} (fake test double)")
        return 0
    if "-accel" in argv and "help" in argv:
        sys.stdout.write(ACCEL_HELP)
        return 0
    if "-device" in argv and "help" in argv:
        sys.stdout.write(DEVICE_HELP)
        return 0

    qemu = FakeQemu(argv)

    def on_signal(signum, _frame):
        qemu.log({"signal": int(signum)})
        os._exit(0)

    for sig in (signal.SIGTERM, signal.SIGINT):
        # Signal handling is not available on every platform/thread; the double works
        # without it.
        with contextlib.suppress(ValueError, OSError):
            signal.signal(sig, on_signal)

    sys.stderr.write(f"fake-qemu: starting with {len(argv)} arguments\n")
    sys.stderr.flush()

    qemu.send(
        {
            "QMP": {
                "version": {
                    "qemu": {"major": 11, "minor": 1, "micro": 2},
                    "package": f"v{VERSION}-fake",
                },
                "capabilities": ["oob"],
            }
        }
    )

    for line in sys.stdin:
        text = line.strip()
        if not text:
            continue
        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict):
            continue
        qemu.handle(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
