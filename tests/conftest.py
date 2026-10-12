"""Shared fixtures: a real fake QEMU, a fake qemu-img, and a fully wired service.

The integration fixtures deliberately start a real child process over real pipes. A fake
QMP object living inside the test process would let a framing bug, a teardown leak or a
missing ``close_fds`` slip through unnoticed, and those are exactly the failures the plan
asks the lifecycle tests to catch.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

import pytest

from palette.audit import NullAuditLog
from palette.config import PaletteConfig, Profile
from palette.policy import PathGuard
from palette.service import PaletteService
from support.profiles import make_profile

SUPPORT = Path(__file__).parent / "support"
FAKE_QEMU = SUPPORT / "fake_qemu.py"
FAKE_QEMU_IMG = SUPPORT / "fake_qemu_img.py"


@pytest.fixture
def runtime_root(tmp_path: Path) -> Path:
    root = tmp_path / "runtime"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def base_image(tmp_path: Path) -> Path:
    """A stand-in base image: the fake qemu-img only cares that it exists and is a file."""
    images = tmp_path / "images"
    images.mkdir(parents=True, exist_ok=True)
    path = images / "base.qcow2"
    path.write_bytes(b"QFI\xfb" + b'{"driver":"qcow2","virtual_size":25769803776}')
    return path


@pytest.fixture
def profile(base_image: Path) -> Profile:
    return make_profile(base_image)


@pytest.fixture
def config(profile: Profile, runtime_root: Path, tmp_path: Path) -> PaletteConfig:
    return PaletteConfig(
        profiles={profile.profile_id: profile},
        config_root=tmp_path,
    ).model_copy(
        update={"host": PaletteConfig().host.model_copy(update={"runtime_root": str(runtime_root)})}
    )


@pytest.fixture
async def service(config: PaletteConfig, runtime_root: Path) -> PaletteService:
    instance = PaletteService(config, audit=NullAuditLog())
    await instance.startup()
    try:
        yield instance
    finally:
        await instance.shutdown()


@pytest.fixture
def guard(runtime_root: Path) -> PathGuard:
    return PathGuard(runtime_root)


@pytest.fixture
async def running_vm(service: PaletteService):
    """A started VM backed by the fake QEMU process, torn down afterwards."""
    instance = await service.manager.start("desktop", "default")
    try:
        yield instance
    finally:
        if service.manager.find("desktop") is not None:
            # Teardown must never mask the failure the test is reporting.
            with contextlib.suppress(Exception):
                await service.manager.stop("desktop", force=True)


def read_command_log(instance) -> list[dict]:
    """Read the fake QEMU's received-command log for one instance."""
    path = Path(f"{instance.paths.overlay}.log.jsonl")
    if not path.is_file():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        records.append(json.loads(line))
    return records


def write_behavior(instance, behavior: dict) -> Path:
    """Tell the running fake QEMU to change its mind about screendumps, resolution, ..."""
    path = Path(f"{instance.paths.overlay}.behavior.json")
    path.write_text(json.dumps(behavior), encoding="utf-8")
    return path


def run(coro):
    """Run a coroutine from a synchronous test."""
    return asyncio.run(coro)
