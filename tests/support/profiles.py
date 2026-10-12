"""Test profiles: production defaults, pointed at the test doubles."""

from __future__ import annotations

import sys
from pathlib import Path

from palette.config import (
    CaptureConfig,
    DisplayConfig,
    GuestToolsConfig,
    ImagesConfig,
    NetworkConfig,
    Profile,
    QemuConfig,
    ResourcesConfig,
    RetentionConfig,
    TextConfig,
    TimeoutConfig,
)

SUPPORT = Path(__file__).resolve().parent
FAKE_QEMU = SUPPORT / "fake_qemu.py"
FAKE_QEMU_IMG = SUPPORT / "fake_qemu_img.py"

#: What the fake QEMU renders. Small enough to keep the suite quick while still exercising
#: scaling, coordinate arithmetic and byte limits.
FAKE_WIDTH = 320
FAKE_HEIGHT = 200


def make_profile(
    base_image: Path,
    *,
    profile_id: str = "default",
    width: int = FAKE_WIDTH,
    height: int = FAKE_HEIGHT,
    **overrides: object,
) -> Profile:
    """A profile pointed at the test doubles, with production defaults intact."""
    qemu = QemuConfig(
        binary=sys.executable,
        argv_prefix=["-I", str(FAKE_QEMU)],
        qemu_img=sys.executable,
        qemu_img_prefix=["-I", str(FAKE_QEMU_IMG)],
        version_pin="11.1",
        accelerator="whpx",
        machine="q35",
    )
    images = ImagesConfig(base=str(base_image), format="qcow2", size_gib=1)
    resources = ResourcesConfig(
        vcpus=1,
        memory_mib=512,
        process_memory_limit_mib=None,
        max_queued_wait_seconds=2.0,
        max_action_seconds=15.0,
    )
    timeouts = TimeoutConfig(
        qmp_command_seconds=5.0,
        qmp_handshake_seconds=5.0,
        startup_seconds=20.0,
        boot_poll_seconds=0.05,
        shutdown_seconds=0.5,
        force_kill_seconds=2.0,
        settle_after_input_ms=10,
    )
    capture = CaptureConfig(
        max_long_edge=width, size_check_interval_seconds=0.0, timeout_seconds=5.0
    )
    data: dict[str, object] = {
        "profile_id": profile_id,
        "qemu": qemu,
        "images": images,
        "resources": resources,
        "timeouts": timeouts,
        "capture": capture,
        "guest_tools": GuestToolsConfig(),
    }
    data.update(overrides)
    data.setdefault("display", DisplayConfig(width=width, height=height))
    data.setdefault("network", NetworkConfig(mode="offline", restrict=True))
    data.setdefault("text", TextConfig(char_interval_ms=1, max_chars=256))
    data.setdefault("retention", RetentionConfig(keep_overlays=True, max_overlays=4))
    return Profile(**data)  # type: ignore[arg-type]
