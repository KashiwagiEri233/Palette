"""Run the existing fake QEMU, recording its PID for owner-process teardown tests."""

from __future__ import annotations

import json
import os
import runpy
import sys
from pathlib import Path

if "-blockdev" in sys.argv:
    for index, argument in enumerate(sys.argv[:-1]):
        if argument == "-blockdev":
            filename = json.loads(sys.argv[index + 1]).get("filename")
            if filename:
                Path(f"{filename}.pid").write_text(str(os.getpid()), encoding="ascii")
                break

runpy.run_path(str(Path(__file__).with_name("fake_qemu.py")), run_name="__main__")
