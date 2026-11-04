"""Run an isolated benchmark owner without starting a VM.

The explicitly confirmed test root must already contain palette.toml and profiles.
Use the normal authenticated OperatorClient to start/stop a disposable VM, then request
runtime.stop once every test VM is stopped. ``serial`` restores the former public
observation transaction: acquisition, PNG encoding, rescaling and thumbnail generation
all remain inside the input FIFO. Both patches apply to this benchmark process only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal, cast

from palette.audit import AuditLog
from palette.capture.base import CaptureBackend, CapturedImage
from palette.config import CaptureConfig, PaletteConfig, load_config
from palette.errors import ErrorCode, PaletteError
from palette.input.controller import ActionController
from palette.input.scheduler import JobKind
from palette.models import Frame
from palette.operator import OperatorClient
from palette.qmp.client import QmpClient
from palette.runtime import RuntimeOwner
from palette.service import PaletteService
from palette.vm import manager

Pipeline = Literal["split", "serial"]


class SerialCapture:
    """Delegate to the existing backend without advertising acquire()/encode()."""

    __slots__ = ("__backend",)

    def __init__(self, backend: CaptureBackend) -> None:
        self.__backend = backend

    async def capture(self, *, display_id: str, head: int, sink: Path) -> CapturedImage:
        return await self.__backend.capture(display_id=display_id, head=head, sink=sink)


@contextmanager
def observation_pipeline(mode: Pipeline) -> Iterator[None]:
    """Patch only this owner's capture factory; retain its sole existing QMP client."""
    factories = vars(manager)
    original_observe = ActionController.observe
    original = cast(
        Callable[[QmpClient, CaptureConfig], CaptureBackend], factories["QmpCaptureBackend"]
    )

    def serial(client: QmpClient, config: CaptureConfig) -> CaptureBackend:
        return SerialCapture(original(client, config))

    async def serial_observe(
        self: ActionController,
        *,
        request_id: str | None = None,
        max_long_edge: int | None = None,
    ) -> tuple[Frame, CapturedImage]:
        started = asyncio.get_running_loop().time()
        job = self.scheduler.make_job(
            JobKind.OBSERVE,
            "observe",
            lambda: self._capture_frame(max_long_edge=max_long_edge),
        )
        result = cast("tuple[Frame, CapturedImage]", await self.scheduler.submit(job))
        # This includes queueing and the complete serialized image transaction.
        self.observation_timings.record(
            total_ms=(asyncio.get_running_loop().time() - started) * 1000
        )
        return result

    if mode == "serial":
        # Benchmark-only constructor replacement hides the optional split interface.
        factories["QmpCaptureBackend"] = serial
        ActionController.observe = serial_observe  # type: ignore[method-assign]
    try:
        yield
    finally:
        if mode == "serial":
            ActionController.observe = original_observe  # type: ignore[method-assign]
            factories["QmpCaptureBackend"] = original


def disposable_config(root: Path, *, disposable: bool) -> PaletteConfig:
    if not disposable:
        raise ValueError("--disposable is required to confirm this is an isolated test root")
    root = root.resolve()
    if not root.is_dir() or not (root / "palette.toml").is_file():
        raise ValueError("--root must be an existing test configuration containing palette.toml")
    config = load_config(root)
    if not config.profiles:
        raise ValueError("the test configuration must contain at least one profile")
    runtime = config.runtime_root.resolve()
    if runtime == root or not runtime.is_relative_to(root):
        raise ValueError("the test runtime_root must be a child directory of the explicit --root")
    return config


async def require_no_owner(config: PaletteConfig) -> None:
    """Never attach to, stop, or replace an existing owner to prepare a benchmark."""
    try:
        async with asyncio.timeout(3):
            await OperatorClient(config.runtime_root).call("hello")
    except PaletteError as error:
        if error.code == ErrorCode.PRECONDITION_FAILED:
            return  # No usable endpoint; RuntimeOwner's OS lease still arbitrates races.
        raise PaletteError(
            ErrorCode.STATE_CONFLICT,
            "the test root has an existing owner endpoint; it must be stopped separately",
        ) from error
    except TimeoutError:
        raise PaletteError(
            ErrorCode.STATE_CONFLICT,
            "the existing test owner endpoint did not respond; refusing to replace it",
        ) from None
    raise PaletteError(
        ErrorCode.STATE_CONFLICT,
        "the test root already has a live owner; it must be stopped separately",
    )


async def serve(config: PaletteConfig, mode: Pipeline) -> None:
    await require_no_owner(config)
    profile = config.profile(None)
    audit = AuditLog(
        config.runtime_root / "audit" / "palette.jsonl",
        max_bytes=profile.retention.audit_max_bytes,
        max_files=profile.retention.audit_max_files,
        enabled=config.host.audit_enabled,
    )
    with observation_pipeline(mode):
        owner = RuntimeOwner(PaletteService(config, audit=audit), kind="benchmark")
        loop = asyncio.get_running_loop()
        signals: list[signal.Signals] = []
        if os.name != "nt":
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, owner.stopping.set)
                signals.append(sig)
        try:
            await owner.start()
            print(
                json.dumps(
                    {
                        "ready": True,
                        "pid": os.getpid(),
                        "root": str(config.config_root),
                        "runtime_root": str(config.runtime_root),
                        "observation_pipeline": mode,
                    }
                ),
                flush=True,
            )
            await owner.stopping.wait()
        finally:
            try:
                await owner.close()
            finally:
                for sig in signals:
                    loop.remove_signal_handler(sig)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--root", type=Path, required=True)
    result.add_argument("--disposable", action="store_true", required=True)
    result.add_argument("--observation-pipeline", choices=("split", "serial"), required=True)
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = parser()
    args = arguments.parse_args(argv)
    try:
        config = disposable_config(args.root, disposable=args.disposable)
        asyncio.run(serve(config, args.observation_pipeline))
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, PaletteError) as error:
        arguments.exit(1, f"{error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
