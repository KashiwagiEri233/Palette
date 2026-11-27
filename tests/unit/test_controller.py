"""Core action transaction contracts: fencing, idempotency, effect states, ledger and queueing."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from palette.audit import NullAuditLog
from palette.config import Profile, ResourcesConfig
from palette.errors import ErrorCode, PaletteError
from palette.input.controller import ActionController, InstanceView
from support.doubles import StubCaptureBackend, StubQmp
from support.profiles import make_profile


class BlockingCapture(StubCaptureBackend):
    """A capture backend that can be held open so scheduling and cancellation can be tested."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.gate = asyncio.Event()
        self.gate.set()
        self.entered = asyncio.Event()
        self.hold_from: int | None = None

    async def capture(self, *, display_id: str, head: int, sink: Path):
        index = self.calls + 1
        if self.hold_from is not None and index >= self.hold_from:
            self.entered.set()
            await self.gate.wait()
        return await StubCaptureBackend.capture(self, display_id=display_id, head=head, sink=sink)

    def hold(self) -> None:
        self.entered.clear()
        self.hold_from = 1
        self.gate.clear()

    def hold_after(self, completed: int) -> None:
        self.entered.clear()
        self.hold_from = completed + 1
        self.gate.clear()

    def release(self) -> None:
        self.gate.set()
        self.hold_from = None


def view(**overrides) -> InstanceView:
    base = {
        "vm_id": "desktop",
        "instance_id": "inst_1",
        "profile_id": "default",
        "display_id": "video0",
        "head": 0,
    }
    base.update(overrides)
    return InstanceView(**base)  # type: ignore[arg-type]


class Harness:
    """A controller wired to in-process doubles, with the queue worker running."""

    def __init__(
        self, profile: Profile, capture: StubCaptureBackend, qmp: StubQmp, frames: Path
    ) -> None:
        self.view = view()
        self.qmp = qmp
        self.capture = capture
        self.controller = ActionController(
            view=self.view,
            profile=profile,
            qmp=qmp,  # type: ignore[arg-type]
            capture=capture,
            frames_dir=frames,
            audit=NullAuditLog(),
        )

    async def __aenter__(self) -> Harness:
        await self.controller.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.controller.shutdown()


@pytest.fixture
def harness_factory(profile: Profile, tmp_path: Path, base_image: Path):
    def build(
        *,
        capture: StubCaptureBackend | None = None,
        qmp: StubQmp | None = None,
        profile_overrides: dict | None = None,
    ) -> Harness:
        built_profile = profile
        if profile_overrides:
            built_profile = make_profile(base_image, **profile_overrides)
        frames = tmp_path / "frames"
        frames.mkdir(parents=True, exist_ok=True)
        return Harness(built_profile, capture or StubCaptureBackend(), qmp or StubQmp(), frames)

    return build


class TestFrameFencing:
    async def test_an_older_frame_is_fenced_by_the_action_sequence(self, harness_factory) -> None:
        async with harness_factory() as h:
            first, _ = await h.controller.observe()
            await h.controller.act(
                request_id="r1",
                frame_id=first.frame_id,
                action_payload={"type": "move", "x": 5, "y": 5},
            )
            receipt, _ = await h.controller.act(
                request_id="r2",
                frame_id=first.frame_id,
                action_payload={"type": "click", "x": 5, "y": 5},
            )
            assert receipt.ok is False
            assert receipt.error is not None
            assert receipt.error.code == ErrorCode.STALE_FRAME
            assert receipt.effect_state == "not_sent"

    async def test_a_frame_from_before_a_handover_is_refused_with_the_right_code(
        self, harness_factory
    ) -> None:
        async with harness_factory() as h:
            frame, _ = await h.controller.observe()
            h.view.controller_epoch += 1
            receipt, _ = await h.controller.act(
                request_id="r1",
                frame_id=frame.frame_id,
                action_payload={"type": "move", "x": 1, "y": 1},
            )
            assert receipt.error is not None
            assert receipt.error.code == ErrorCode.CONTROL_NOT_OWNED

    async def test_a_resize_is_caught_before_any_input_is_sent(self, harness_factory) -> None:
        capture = StubCaptureBackend()
        async with harness_factory(capture=capture) as h:
            frame, _ = await h.controller.observe()
            capture.width, capture.height = 400, 300
            receipt, image = await h.controller.act(
                request_id="r1",
                frame_id=frame.frame_id,
                action_payload={"type": "click", "x": 10, "y": 10},
            )
            assert receipt.ok is False
            assert receipt.error is not None
            assert receipt.error.code == ErrorCode.STALE_FRAME
            assert receipt.effect_state == "not_sent"
            assert image is None
            assert h.qmp.events() == []
            assert h.view.display_epoch == 2


class TestIdempotency:
    async def test_the_same_request_id_with_different_parameters_is_a_conflict(
        self, harness_factory
    ) -> None:
        async with harness_factory() as h:
            frame, _ = await h.controller.observe()
            await h.controller.act(
                request_id="same",
                frame_id=frame.frame_id,
                action_payload={"type": "move", "x": 5, "y": 5},
            )
            with pytest.raises(PaletteError) as info:
                await h.controller.act(
                    request_id="same",
                    frame_id=frame.frame_id,
                    action_payload={"type": "move", "x": 6, "y": 5},
                )
            assert info.value.code is ErrorCode.REQUEST_ID_CONFLICT

    async def test_de_duplication_is_checked_before_the_stale_frame_check(
        self, harness_factory
    ) -> None:
        async with harness_factory() as h:
            frame, _ = await h.controller.observe()
            payload = {"type": "move", "x": 1, "y": 1}
            await h.controller.act(request_id="r1", frame_id=frame.frame_id, action_payload=payload)
            replay, _ = await h.controller.act(
                request_id="r1", frame_id=frame.frame_id, action_payload=payload
            )
            assert replay.ok is True
            assert replay.deduplicated is True


class TestEffectStatesAndLedger:
    async def test_a_failure_midway_is_partial(self, harness_factory) -> None:
        qmp = StubQmp()
        qmp.failures["input-send-event"] = PaletteError(ErrorCode.QMP_COMMAND_FAILED, "pipe died")
        qmp.fail_after["input-send-event"] = 2
        async with harness_factory(qmp=qmp) as h:
            frame, _ = await h.controller.observe()
            receipt, _ = await h.controller.act(
                request_id="r1",
                frame_id=frame.frame_id,
                action_payload={"type": "click", "x": 1, "y": 1},
            )
            assert receipt.effect_state == "partial"
            assert receipt.input_submitted is True
            assert h.controller.ledger_snapshot()["buttons"] == ["left"]

    async def test_a_timeout_is_reported_as_an_unknown_outcome(self, harness_factory) -> None:
        capture = BlockingCapture()
        overrides = {
            "resources": ResourcesConfig(
                vcpus=1, memory_mib=512, process_memory_limit_mib=None, max_action_seconds=0.15
            )
        }
        async with harness_factory(capture=capture, profile_overrides=overrides) as h:
            frame, _ = await h.controller.observe()
            capture.hold_after(2)
            result, _ = await h.controller.act(
                request_id="r1",
                frame_id=frame.frame_id,
                action_payload={"type": "move", "x": 1, "y": 1},
            )
            assert result.error is not None
            assert result.error.code == ErrorCode.ACTION_OUTCOME_UNKNOWN
            assert result.effect_state == "submitted"
            replay, png = await h.controller.act(
                request_id="r1",
                frame_id=frame.frame_id,
                action_payload={"type": "move", "x": 1, "y": 1},
            )
            assert replay.deduplicated and png is None
            assert h.qmp.absolute() == [(103, 165)]
            capture.release()

    async def test_a_stuck_button_is_remembered_until_it_is_released(self, harness_factory) -> None:
        qmp = StubQmp()
        qmp.failures["input-send-event"] = PaletteError(ErrorCode.QMP_UNAVAILABLE, "gone")
        qmp.fail_after["input-send-event"] = 2
        async with harness_factory(qmp=qmp) as h:
            frame, _ = await h.controller.observe()
            await h.controller.act(
                request_id="r1",
                frame_id=frame.frame_id,
                action_payload={"type": "click", "x": 1, "y": 1},
            )
            assert h.controller.ledger_snapshot()["buttons"] == ["left"]

            qmp.failures.clear()
            qmp.fail_after.clear()
            result = await h.controller.release_inputs()
            assert result.ok is True
            assert result.release_confirmed is True
            assert result.released_buttons == ["left"]
            assert h.controller.ledger_snapshot()["buttons"] == []

    async def test_emergency_release_cancels_a_running_action_and_lets_go(
        self, harness_factory
    ) -> None:
        capture = BlockingCapture()
        async with harness_factory(capture=capture) as h:
            frame, _ = await h.controller.observe()
            capture.hold_after(2)
            task = asyncio.create_task(
                h.controller.act(
                    request_id="r1",
                    frame_id=frame.frame_id,
                    action_payload={"type": "click", "x": 1, "y": 1},
                )
            )
            await capture.entered.wait()
            result = await h.controller.emergency_release()
            assert result.ok is True
            assert ("left", False) in h.qmp.buttons()
            interrupted, _ = await task
            assert not interrupted.ok
            assert interrupted.frame is None
            assert h.controller.ledger_snapshot() == {"keys": [], "buttons": []}
            capture.release()


class TestCancellationAndQueueing:
    async def test_cancelled_queued_action_never_reaches_qmp(self, harness_factory) -> None:
        capture = BlockingCapture()
        async with harness_factory(capture=capture) as h:
            frame, _ = await h.controller.observe()
            capture.hold()
            blocker = asyncio.create_task(h.controller.observe())
            await capture.entered.wait()
            action = asyncio.create_task(
                h.controller.act(
                    request_id="cancel-queued",
                    frame_id=frame.frame_id,
                    action_payload={"type": "key", "keys": ["ENTER"]},
                )
            )
            await asyncio.sleep(0)
            action.cancel()
            with pytest.raises(asyncio.CancelledError):
                await action
            capture.release()
            await blocker
            await h.controller.observe()
            assert h.qmp.events() == []

    async def test_cancelled_running_request_keeps_terminal_receipt(self, harness_factory) -> None:
        pressed = asyncio.Event()

        class NotifyingQmp(StubQmp):
            async def execute(self, *args, **kwargs):
                result = await super().execute(*args, **kwargs)
                if self.qcodes():
                    pressed.set()
                return result

        async with harness_factory(qmp=NotifyingQmp()) as h:
            frame, _ = await h.controller.observe()
            arguments = {
                "request_id": "cancel-running",
                "frame_id": frame.frame_id,
                "action_payload": {"type": "key", "keys": ["ENTER"], "hold_ms": 500},
            }
            first = asyncio.create_task(h.controller.act(**arguments))
            await pressed.wait()
            duplicate = asyncio.create_task(h.controller.act(**arguments))
            await asyncio.sleep(0)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            completed, _ = await duplicate
            assert completed.deduplicated
            assert completed.effect_state == "partial"
            assert completed.error is not None
            assert completed.error.code == ErrorCode.CANCELLED
            replay, image = await h.controller.act(**arguments)
            assert replay.deduplicated and image is None
            assert h.qmp.qcodes() == [("ret", True), ("ret", False)]

    async def test_a_full_queue_is_reported_as_busy(self, harness_factory) -> None:
        capture = BlockingCapture()
        overrides = {
            "resources": ResourcesConfig(
                vcpus=1, memory_mib=512, process_memory_limit_mib=None, max_queue_depth=1
            )
        }
        async with harness_factory(capture=capture, profile_overrides=overrides) as h:
            capture.hold()
            blocker = asyncio.create_task(h.controller.observe())
            await capture.entered.wait()
            queued = asyncio.create_task(h.controller.observe())
            await asyncio.sleep(0.02)
            with pytest.raises(PaletteError) as info:
                await h.controller.observe()
            assert info.value.code is ErrorCode.BUSY
            capture.release()
            await blocker
            await queued
