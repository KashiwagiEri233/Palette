"""Public observations preserve capture evidence while image encoding yields input."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest

from palette.capture.base import AcquiredImage, CapturedImage
from palette.capture.qmp import QmpCaptureBackend
from palette.config import Profile
from palette.errors import ErrorCode, PaletteError
from palette.input.controller import ActionController
from palette.input.events import key_event
from palette.input.state import InstanceView
from palette.models import Size
from support.doubles import StubQmp, render_png


class CaptureQmp(StubQmp):
    def __init__(self) -> None:
        super().__init__()
        self.width = 320

    async def execute(self, command, arguments=None, **kwargs):
        result = await super().execute(command, arguments, **kwargs)
        if command == "screendump":
            Path(arguments["filename"]).write_bytes(render_png(self.width, 200))
        return result


class GatedCapture(QmpCaptureBackend):
    def __init__(self, qmp: CaptureQmp, profile: Profile) -> None:
        super().__init__(qmp, profile.capture)  # type: ignore[arg-type]
        self.loop = asyncio.get_running_loop()
        self.acquisitions: list[AcquiredImage] = []
        self.entered = {index: asyncio.Event() for index in range(1, 10)}
        self.gates: dict[int, threading.Event] = {}
        self.finished: set[int] = set()

    async def acquire(self, **kwargs) -> AcquiredImage:
        image = await super().acquire(**kwargs)
        self.acquisitions.append(image)
        return image

    def block(self, index: int) -> None:
        self.gates[index] = threading.Event()

    def encode(self, acquired: AcquiredImage) -> CapturedImage:
        index = self.acquisitions.index(acquired) + 1
        self.loop.call_soon_threadsafe(self.entered[index].set)
        if index in self.gates:
            assert self.gates[index].wait(5), "test did not release the encoder"
        result = super().encode(acquired)
        self.finished.add(index)
        return result


@pytest.fixture
async def pipeline(profile: Profile, tmp_path: Path):
    qmp = CaptureQmp()
    capture = GatedCapture(qmp, profile)
    controller = ActionController(
        view=InstanceView("desktop", "inst_1", "default", "video0", 0, state="RUNNING"),
        profile=profile,
        qmp=qmp,  # type: ignore[arg-type]
        capture=capture,
        frames_dir=tmp_path,
    )
    await controller.start()
    try:
        yield controller, capture, qmp
    finally:
        for gate in capture.gates.values():
            gate.set()
        await controller.shutdown()


async def entered(capture: GatedCapture, index: int) -> None:
    await asyncio.wait_for(capture.entered[index].wait(), 2)


async def test_input_proceeds_during_encoding_and_frame_keeps_acquisition_evidence(pipeline):
    controller, capture, _qmp = pipeline
    capture.block(1)
    observation = asyncio.create_task(controller.observe())
    await entered(capture, 1)
    assert controller.view.native_size == Size(width=320, height=200)
    acquired_before = time.monotonic()
    sequence = await asyncio.wait_for(
        controller.operator_input([key_event("a", True), key_event("a", False)], lambda: None), 1
    )
    assert sequence == 1
    assert not observation.done()
    capture.gates[1].set()
    frame, image = await observation
    assert frame.action_seq == 0
    assert frame.monotonic_stamp <= acquired_before
    assert image.png_bytes.startswith(b"\x89PNG")
    with pytest.raises(PaletteError) as error:
        controller._validate_frame_for_action(frame)
    assert error.value.code == ErrorCode.STALE_FRAME
    assert set(controller.observation_timings.snapshot()) == {
        "queue_ms",
        "acquire_ms",
        "encode_ms",
        "total_ms",
    }


async def test_late_old_encoding_does_not_replace_action_followup_frame(pipeline):
    controller, capture, _qmp = pipeline
    before, _ = await controller.observe()
    capture.block(2)
    old = asyncio.create_task(controller.observe())
    await entered(capture, 2)
    receipt, _ = await controller.act(
        request_id="action-with-followup",
        frame_id=before.frame_id,
        action_payload={"type": "move", "x": 1, "y": 1},
    )
    assert receipt.ok and receipt.frame is not None
    capture.gates[2].set()
    old_frame, _ = await old
    assert old_frame.action_seq == 0
    assert receipt.frame.action_seq == 1
    assert controller.latest_frame == receipt.frame


async def test_only_two_snapshots_are_acquired_while_encoders_are_busy(pipeline):
    controller, capture, qmp = pipeline
    capture.block(1)
    capture.block(2)
    first = asyncio.create_task(controller.observe())
    second = asyncio.create_task(controller.observe())
    await entered(capture, 1)
    await entered(capture, 2)
    third = asyncio.create_task(controller.observe())
    await asyncio.sleep(0.01)
    assert len(capture.acquisitions) == 2
    await asyncio.wait_for(controller.operator_input([key_event("a", False)], lambda: None), 1)
    assert qmp.qcodes() == [("a", False)]
    capture.gates[1].set()
    await first
    await entered(capture, 3)
    capture.gates[2].set()
    await asyncio.gather(second, third)


async def test_cancelled_encoding_retains_capacity_until_thread_finishes(pipeline):
    controller, capture, _qmp = pipeline
    capture.block(1)
    capture.block(2)
    first = asyncio.create_task(controller.observe())
    second = asyncio.create_task(controller.observe())
    await entered(capture, 1)
    await entered(capture, 2)
    first.cancel()
    third = asyncio.create_task(controller.observe())
    await asyncio.sleep(0.01)
    assert not first.done()
    assert len(capture.acquisitions) == 2
    capture.gates[1].set()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert 1 in capture.finished
    await entered(capture, 3)
    capture.gates[2].set()
    await asyncio.gather(second, third)
    assert len(controller._frames._frames) == 2


@pytest.mark.parametrize("change", ["instance_id", "boot_epoch", "controller_epoch", "generation"])
async def test_encoding_cannot_publish_across_identity_changes(pipeline, change):
    controller, capture, _qmp = pipeline
    capture.block(1)
    observation = asyncio.create_task(controller.observe())
    await entered(capture, 1)
    if change == "generation":
        controller.invalidate(reason="takeover")
    elif change == "instance_id":
        controller.view.instance_id = "inst_2"
    else:
        setattr(controller.view, change, getattr(controller.view, change) + 1)
    capture.gates[1].set()
    with pytest.raises(PaletteError) as error:
        await observation
    assert error.value.code == ErrorCode.STALE_FRAME
    assert controller.latest_frame is None


async def test_resize_is_committed_before_encoding_and_old_completion_cannot_undo_it(pipeline):
    controller, capture, qmp = pipeline
    capture.block(1)
    old = asyncio.create_task(controller.observe())
    await entered(capture, 1)
    qmp.width = 640
    current, _ = await controller.observe()
    assert controller.view.native_size == Size(width=640, height=200)
    assert current.display_epoch == 2
    capture.gates[1].set()
    with pytest.raises(PaletteError) as error:
        await old
    assert error.value.code == ErrorCode.STALE_FRAME
    assert controller.latest_frame == current
    assert controller.view.native_size == current.native_size


async def test_shutdown_releases_input_then_joins_encoding_without_late_publication(pipeline):
    controller, capture, qmp = pipeline
    await controller.operator_input([key_event("a", True)], lambda: None)
    capture.block(1)
    observation = asyncio.create_task(controller.observe())
    await entered(capture, 1)
    closing = asyncio.create_task(controller.shutdown())
    await asyncio.sleep(0.02)
    assert qmp.qcodes() == [("a", True), ("a", False)]
    assert not closing.done()
    capture.gates[1].set()
    await closing
    result = await asyncio.gather(observation, return_exceptions=True)
    assert isinstance(result[0], (asyncio.CancelledError, PaletteError))
    assert 1 in capture.finished
    assert controller.latest_frame is None


async def test_cancellation_and_shutdown_join_acquisition_file_reader(pipeline, monkeypatch):
    controller, capture, _qmp = pipeline
    reading = asyncio.Event()
    gate = threading.Event()
    original = capture._read_and_cleanup

    def read(sink):
        capture.loop.call_soon_threadsafe(reading.set)
        assert gate.wait(5), "test did not release the file reader"
        return original(sink)

    monkeypatch.setattr(capture, "_read_and_cleanup", read)
    observation = asyncio.create_task(controller.observe())
    closing = None
    try:
        await asyncio.wait_for(reading.wait(), 2)
        observation.cancel()
        closing = asyncio.create_task(controller.shutdown())
        await asyncio.sleep(0.02)
        assert not observation.done()
        assert not closing.done()
        assert list(controller._frames_dir.glob("frame-*.png"))
    finally:
        gate.set()
        await asyncio.gather(observation, return_exceptions=True)
        if closing is not None:
            await closing
    assert not list(controller._frames_dir.glob("frame-*.png"))
    assert controller.latest_frame is None


async def test_registry_evicts_old_acquisitions_even_when_they_finish_late(pipeline):
    controller, _capture, _qmp = pipeline
    frame, _ = await controller.observe()
    registry = controller._frames
    registry._max_entries = 2
    second = frame.model_copy(update={"frame_id": "second"})
    third = frame.model_copy(update={"frame_id": "third"})
    old = frame.model_copy(update={"frame_id": "late-old"})
    registry.add(second, sequence=2)
    registry.add(third, sequence=3)
    registry.add(old, sequence=0)
    assert registry.latest() == third
    assert registry.get(second.frame_id) == second
    with pytest.raises(PaletteError) as error:
        registry.get(old.frame_id)
    assert error.value.code == ErrorCode.FRAME_NOT_FOUND


async def test_encoding_uses_remaining_observation_deadline(pipeline):
    controller, capture, _qmp = pipeline
    controller.profile = controller.profile.model_copy(
        update={
            "resources": controller.profile.resources.model_copy(
                update={"max_action_seconds": 0.05}
            )
        }
    )
    capture.block(1)
    observation = asyncio.create_task(controller.observe())
    await entered(capture, 1)
    await asyncio.sleep(0.08)
    assert not observation.done()  # deadline still joins the image worker
    capture.gates[1].set()
    with pytest.raises(PaletteError) as error:
        await observation
    assert error.value.code == ErrorCode.TIMEOUT
    assert controller.latest_frame is None
