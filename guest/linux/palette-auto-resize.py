#!/usr/bin/env python3
"""Apply virtio-gpu preferred modes in the interactive X11 session, without root."""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import time

XRANDR = "/usr/bin/xrandr"
PORT = "/dev/virtio-ports/org.palette.desktop.0"


def preferred_modes(output: str) -> dict[str, tuple[str, bool]]:
    result: dict[str, tuple[str, bool]] = {}
    name: str | None = None
    for line in output.splitlines():
        if line and not line[0].isspace():
            words = line.split()
            name = (
                words[0]
                if len(words) > 1
                and words[1] == "connected"
                and re.fullmatch(r"(?:Virtual|Virtio)-?\d+", words[0])
                else None
            )
        elif name is not None:
            words = line.split()
            if words and re.fullmatch(r"\d+x\d+", words[0]) and "+" in line:
                result[name] = words[0], "*" in line
    return result


def main() -> None:
    if os.environ.get("XDG_SESSION_TYPE") == "wayland" or not os.environ.get("DISPLAY"):
        return
    previous: dict[str, str] = {}
    fd: int | None = None
    try:
        while True:
            try:
                query = subprocess.run(  # noqa: S603 - fixed executable and arguments
                    [XRANDR, "--query"], capture_output=True, text=True, timeout=3, check=True
                )
                modes = preferred_modes(query.stdout)
                for name, (mode, current) in modes.items():
                    if not current and previous.get(name) != mode:
                        subprocess.run(  # noqa: S603 - validated output and mode
                            [XRANDR, "--output", name, "--mode", mode],
                            capture_output=True,
                            timeout=3,
                            check=True,
                        )
                    previous[name] = mode
                previous = {name: mode for name, mode in previous.items() if name in modes}
                if modes:
                    if fd is None:
                        fd = os.open(PORT, os.O_RDWR | os.O_NONBLOCK)
                    heartbeat = (
                        json.dumps({"version": 1, "session": "x11", "resize": True}).encode()
                        + b"\n"
                    )
                    os.write(fd, heartbeat)
            except (OSError, subprocess.SubprocessError):
                if fd is not None:
                    os.close(fd)
                    fd = None
            time.sleep(2)
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)


if __name__ == "__main__":
    main()
