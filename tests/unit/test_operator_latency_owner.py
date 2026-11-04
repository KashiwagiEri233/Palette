"""Benchmark-only serial capture never creates another management connection."""

from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest
from scripts import operator_latency_owner as benchmark

from palette.capture.base import AcquiredImage, CapturedImage, SplitCaptureBackend
from palette.capture.work import image_work
from palette.config import PaletteConfig
from palette.errors import ErrorCode, PaletteError
from palette.input.controller import ActionController
from palette.input.events import key_event
from palette.input.state import InstanceView
from palette.models import Size
from support.doubles import StubCaptureBackend, StubQmp, render_png


@pytest.mark.parametrize(
    "args",
    [
        ["--disposable", "--observation-pipeline", "split"],
        ["--root", ".", "--observation-pipeline", "split"],
        ["--root", ".", "--disposable"],
    ],
)
def test_parser_requires_explicit_root_confirmation_and_pipeline(args):
    with pytest.raises(SystemExit):
        benchmark.parser().parse_args(args)


def test_config_must_exist_and_keep_its_runtime_inside_the_disposable_root(
    tmp_path, config, monkeypatch
):
    with pytest.raises(ValueError, match="disposable"):
        benchmark.disposable_config(tmp_path, disposable=False)
    with pytest.raises(ValueError, match=r"palette\.toml"):
        benchmark.disposable_config(tmp_path, disposable=True)
    (tmp_path / "palette.toml").write_text("[host]\n", encoding="utf-8")
    monkeypatch.setattr(benchmark, "load_config", lambda root: config)
    assert benchmark.disposable_config(tmp_path, disposable=True) is config
    outside = config.model_copy(
        update={
            "host": config.host.model_copy(update={"runtime_root": str(tmp_path.parent / "other")})
        }
    )
    monkeypatch.setattr(benchmark, "load_config", lambda root: outside)
    with pytest.raises(ValueError, match="child directory"):
        benchmark.disposable_config(tmp_path, disposable=True)


@pytest.mark.parametrize("existing", ["live", "ambiguous", "timeout", "missing"])
async def test_live_or_uncertain_owner_is_never_replaced(config, monkeypatch, existing):
    class Client:
        async def call(self, method):
            assert method == "hello"
            if existing == "missing":
                raise PaletteError(ErrorCode.PRECONDITION_FAILED, "absent")
            if existing == "ambiguous":
                raise PaletteError(ErrorCode.ACTION_OUTCOME_UNKNOWN, "disconnected")
            if existing == "timeout":
                raise TimeoutError
            return {"desktop_protocol": 1}

    monkeypatch.setattr(benchmark, "OperatorClient", lambda root: Client())
    if existing == "missing":
        await benchmark.require_no_owner(config)
    else:
        with pytest.raises(PaletteError) as error:
            await benchmark.require_no_owner(config)
        assert error.value.code == ErrorCode.STATE_CONFLICT


def test_serial_factory_uses_the_original_client_and_restores_after_failure(monkeypatch):
    clients = []
    original_observe = ActionController.observe

    class Backend:
        def __init__(self, client, config):
            clients.append(client)

        async def capture(self, **kwargs):
            return None

        async def acquire(self, **kwargs):
            return None

        def encode(self, acquired):
            return None

    monkeypatch.setattr(benchmark.manager, "QmpCaptureBackend", Backend)
    client = object()
    with pytest.raises(RuntimeError), benchmark.observation_pipeline("serial"):
        assert ActionController.observe is not original_observe
        backend = benchmark.manager.QmpCaptureBackend(client, object())
        assert clients == [client]
        assert not isinstance(backend, SplitCaptureBackend)
        assert not hasattr(backend, "acquire")
        assert not hasattr(backend, "encode")
        raise RuntimeError("owner startup failed")
    assert benchmark.manager.QmpCaptureBackend is Backend
    assert ActionController.observe is original_observe
    with benchmark.observation_pipeline("split"):
        assert benchmark.manager.QmpCaptureBackend is Backend
        assert ActionController.observe is original_observe


@pytest.mark.parametrize("mode", ["serial", "split"])
async def test_serial_png_encoding_stays_inside_fifo_while_split_yields_input(
    mode, profile, tmp_path
):
    gate = threading.Event()
    entered = asyncio.Event()
    loop = asyncio.get_running_loop()
    payload = render_png(320, 200)

    class Backend:
        async def acquire(self, **kwargs):
            return AcquiredImage(payload, Size(width=320, height=200), "png", time.perf_counter())

        def encode(self, acquired):
            loop.call_soon_threadsafe(entered.set)
            assert gate.wait(5), "test did not release PNG encoding"
            return CapturedImage(payload, acquired.native_size, acquired.native_size, "png")

        async def capture(self, **kwargs):
            acquired = await self.acquire(**kwargs)
            return await image_work(lambda: self.encode(acquired))

    backend = Backend()
    capture = benchmark.SerialCapture(backend) if mode == "serial" else backend
    qmp = StubQmp()
    controller = ActionController(
        view=InstanceView("desktop", "inst_1", "default", "video0", 0, state="RUNNING"),
        profile=profile,
        capture=capture,
        qmp=qmp,  # type: ignore[arg-type]
        frames_dir=tmp_path,
    )
    await controller.start()
    observation = asyncio.create_task(controller.observe())
    input_task = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        input_task = asyncio.create_task(
            controller.operator_input([key_event("a", False)], lambda: None)
        )
        if mode == "serial":
            await asyncio.sleep(0.02)
            assert not input_task.done()
            assert qmp.events() == []
        else:
            assert await asyncio.wait_for(asyncio.shield(input_task), 1) == 1
        gate.set()
        await observation
        assert await input_task == 1
        assert qmp.qcodes() == [("a", False)]
    finally:
        gate.set()
        await asyncio.gather(observation, return_exceptions=True)
        if input_task is not None:
            await asyncio.gather(input_task, return_exceptions=True)
        await controller.shutdown()


@pytest.mark.parametrize("mode", ["serial", "split"])
async def test_serial_restores_the_whole_observation_transaction_including_thumbnail(
    mode, profile, tmp_path, monkeypatch
):
    from palette.input import controller as controller_module

    original_observe = ActionController.observe
    original_thumbnail = controller_module.image_thumbnail
    loop = asyncio.get_running_loop()
    thumbnail_entered = asyncio.Event()
    gate = threading.Event()

    def thumbnail(png):
        loop.call_soon_threadsafe(thumbnail_entered.set)
        assert gate.wait(5), "test did not release thumbnail processing"
        return original_thumbnail(png)

    monkeypatch.setattr(controller_module, "image_thumbnail", thumbnail)
    qmp = StubQmp()
    controller = ActionController(
        view=InstanceView("desktop", "inst_1", "default", "video0", 0, state="RUNNING"),
        profile=profile,
        capture=StubCaptureBackend(),
        qmp=qmp,  # type: ignore[arg-type]
        frames_dir=tmp_path,
    )
    with benchmark.observation_pipeline(mode):
        await controller.start()
        observation = asyncio.create_task(controller.observe(max_long_edge=160))
        input_task = None
        try:
            await asyncio.wait_for(thumbnail_entered.wait(), 2)
            input_task = asyncio.create_task(
                controller.operator_input([key_event("a", False)], lambda: None)
            )
            if mode == "serial":
                await asyncio.sleep(0.02)
                assert not input_task.done()
                assert qmp.events() == []
            else:
                assert await asyncio.wait_for(asyncio.shield(input_task), 1) == 1
            gate.set()
            frame, _image = await observation
            assert frame.image_size == Size(width=160, height=100)
            assert await input_task == 1
            if mode == "serial":
                assert set(controller.observation_timings.snapshot()) == {"total_ms"}
        finally:
            gate.set()
            await asyncio.gather(observation, return_exceptions=True)
            if input_task is not None:
                await asyncio.gather(input_task, return_exceptions=True)
            await controller.shutdown()
    assert ActionController.observe is original_observe


@pytest.mark.parametrize("startup_fails", [False, True])
async def test_owner_uses_standard_lifecycle_without_starting_a_vm(
    config: PaletteConfig, monkeypatch, capsys, startup_fails
):
    original = benchmark.manager.QmpCaptureBackend
    calls = []

    async def available(config):
        calls.append("check-owner")

    class Owner:
        def __init__(self, service, *, kind):
            assert kind == "benchmark"
            self.stopping = asyncio.Event()

        async def start(self):
            assert benchmark.manager.QmpCaptureBackend is not original
            calls.append("start-owner")
            if startup_fails:
                raise RuntimeError("startup failed")
            self.stopping.set()

        async def close(self):
            calls.append("close-owner")

    monkeypatch.setattr(benchmark, "require_no_owner", available)
    monkeypatch.setattr(benchmark, "PaletteService", lambda config, audit: object())
    monkeypatch.setattr(benchmark, "RuntimeOwner", Owner)
    if startup_fails:
        with pytest.raises(RuntimeError, match="startup failed"):
            await benchmark.serve(config, "serial")
    else:
        await benchmark.serve(config, "serial")
        ready = json.loads(capsys.readouterr().out)
        assert ready["observation_pipeline"] == "serial"
        assert ready["runtime_root"] == str(config.runtime_root)
        assert ready["ready"] is True
    assert calls == ["check-owner", "start-owner", "close-owner"]
    assert benchmark.manager.QmpCaptureBackend is original


async def test_owner_cancellation_closes_before_restoring_capture_factory(config, monkeypatch):
    original = benchmark.manager.QmpCaptureBackend
    started = asyncio.Event()
    closed = asyncio.Event()

    async def available(config):
        return None

    class Owner:
        def __init__(self, service, *, kind):
            self.stopping = asyncio.Event()

        async def start(self):
            started.set()

        async def close(self):
            assert benchmark.manager.QmpCaptureBackend is not original
            closed.set()

    monkeypatch.setattr(benchmark, "require_no_owner", available)
    monkeypatch.setattr(benchmark, "PaletteService", lambda config, audit: object())
    monkeypatch.setattr(benchmark, "RuntimeOwner", Owner)
    task = asyncio.create_task(benchmark.serve(config, "serial"))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
    assert benchmark.manager.QmpCaptureBackend is original
