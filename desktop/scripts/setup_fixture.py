"""Generate a disposable configuration for the supervised fake QEMU."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from palette.config import Profile

root = Path(sys.argv[1]).resolve()
root.mkdir(parents=True, exist_ok=True)
(root / "profiles").mkdir(exist_ok=True)
support = Path(__file__).resolve().parents[2] / "tests" / "support"
base = root / "base.qcow2"
base.write_bytes(b'QFI\xfb{"driver":"qcow2","virtual_size":1073741824}')
profile = Profile.model_validate(
    {
        "profile_id": "default",
        "qemu": {
            "binary": sys.executable,
            "qemu_img": sys.executable,
            "argv_prefix": ["-I", str(support / "fake_qemu.py")],
            "qemu_img_prefix": ["-I", str(support / "fake_qemu_img.py")],
            "accelerator": "tcg",
            "version_pin": "11.1",
        },
        "images": {"base": str(base), "size_gib": 24},
        "resources": {"vcpus": 1, "memory_mib": 512, "process_memory_limit_mib": None},
        "timeouts": {"shutdown_seconds": 2, "settle_after_input_ms": 10},
        "guest_tools": {"enabled": False},
        "network": {"mode": "offline"},
    }
)
toml = ""
for key, value in sorted(
    profile.model_dump(mode="json").items(), key=lambda item: isinstance(item[1], dict)
):
    if isinstance(value, dict):
        toml += f"\n[{key}]\n"
        for field, scalar in value.items():
            if scalar is not None and not isinstance(scalar, dict):
                toml += f"{field} = {json.dumps(scalar)}\n"
    elif value is not None:
        toml += f"{key} = {json.dumps(value)}\n"
(root / "profiles" / "default.toml").write_text(toml, encoding="utf-8")
(root / "palette.toml").write_text(
    '[host]\ndefault_profile = "default"\nruntime_root = ".runtime"\n', encoding="utf-8"
)
