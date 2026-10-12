"""The action transaction: fencing, de-duplication, batching, ledgers and cleanup.

These tests are where the plan's hard requirements are pinned down: a stale frame must
not become a click, a repeated request must not become a second click, a timeout must not
become a silent retry, and a cancelled drag must not leave the mouse button down.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from palette.audit import NullAuditLog
from palette.config import CaptureConfig, Profile, ResourcesConfig
from palette.errors import ErrorCode, PaletteError
from palette.input.controller import ActionController, FrameRegistry, InstanceView
from palette.models import Frame, Size
from support.doubles import StubCaptureBackend, StubQmp
from support.profiles import make_profile


class BlockingCapture(StubCaptureBackend):
    """A capture backend that can be held open, so scheduling can be observed.

    The gate starts open, so a test can take one unobstructed observation first and only
    then hold the next capture.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.gate = asyncio.Event()
        self.gate.set()
        self.entered = asyncio.Event()

    async def capture(self, *, display_id: str, head: int, sink: Path):
        self.entered.set()
        await self.gate.wait()
        return await StubCaptureBackend.capture(self, display_id=display_id, head=head, sink=sink)

    def hold(self) -> None:
        self.entered.clear()
        self.gate.clear()

    def release(self) -> None:
        self.gate.set()


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
    """Build a controller over the in-process doubles.

    Each test owns its harness through ``async with``, which is what guarantees the worker
    task is stopped even when an assertion fails halfway through an action.
    """

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


# ------------------------------------------------------------------------------------


class TestObservation:
    async def test_observe_returns_a_frame_describing_the_delivered_image(
        self, harness_factory
    ) -> None:
        async with harness_factory() as h:
            frame, captured = await h.controller.observe()
            assert frame.native_size == Size(width=320, height=200)
            assert frame.image_size == frame.native_size
            assert frame.transform_id == "full_320x200"
            assert frame.instance_id == "inst_1"
            assert frame.action_seq == 0
            assert frame.cursor_position_source == "not_observed"
            assert captured.png_bytes.startswith(b"\x89PNG")

    async def test_the_frame_is_registered_for_later_actions(self, harness_factory) -> None:
        async with harness_factory() as h:
            frame, _ = await h.controller.observe()
            assert h.controller.frame_by_id(frame.frame_id).frame_id == frame.frame_id
            assert h.controller.latest_frame is not None

    async def test_a_resolution_change_bumps_the_display_epoch(self, harness_factory) -> None:
        async with harness_factory() as h:
            await h.controller.observe()
            epoch = h.view.display_epoch
            h.capture.width, h.capture.height = 400, 300
            frame, _ = await h.controller.observe()
            assert h.view.display_epoch == epoch + 1
            assert frame.display_epoch == epoch + 1
            assert frame.native_size == Size(width=400, height=300)

    async def test_an_unexpected_resolution_is_still_reported_truthfully(
        self, harness_factory
    ) -> None:
        """The profile says 1280x800 and the guest says 320x200: the frame records what
        was actually captured, and the mismatch is logged rather than corrected."""
        async with harness_factory() as h:
            frame, _ = await h.controller.observe()
            assert frame.native_size == Size(width=320, height=200)


class TestFrameFencing:
    async def test_an_unknown_frame_is_refused(self, harness_factory) -> None:
        async with harness_factory() as h:
            await h.controller.observe()
            receipt, image = await h.controller.act(
                request_id="r1",
                frame_id="frame_nope",
                action_payload={"type": "move", "x": 1, "y": 1},
            )
            # A frame the service never issued is a refusal, reported as a receipt with
            # the same shape as any other outcome rather than as a raised exception.
            assert receipt.ok is False
            assert receipt.error is not None
            assert receipt.error.code == ErrorCode.FRAME_NOT_FOUND
            assert receipt.effect_state == "not_sent"
            assert image is None
            assert h.qmp.events() == []

    async def test_an_older_frame_is_fenced_by_the_action_sequence(self, harness_factory) -> None:
        async with harness_factory() as h:
            first, _ = await h.controller.observe()
            await h.controller.act(
                request_id="r1",
                frame_id=first.frame_id,
                action_payload={"type": "move", "x": 5, "y": 5},
            )
            # The desktop changed when that action was submitted, so the old frame must not
            # drive another click even though it is only milliseconds old.
            receipt, _ = await h.controller.act(
                request_id="r2",
                frame_id=first.frame_id,
                action_payload={"type": "click", "x": 5, "y": 5},
            )
            assert receipt.ok is False
            assert receipt.error is not None
            assert receipt.error.code == ErrorCode.STALE_FRAME
            assert receipt.effect_state == "not_sent"

    async def test_a_frame_older_than_the_ttl_is_refused(self, harness_factory) -> None:
        async with harness_factory(profile_overrides={"frame_ttl_seconds": 0.05}) as h:
            frame, _ = await h.controller.observe()
            await asyncio.sleep(0.12)
            receipt, _ = await h.controller.act(
                request_id="r1",
                frame_id=frame.frame_id,
                action_payload={"type": "move", "x": 1, "y": 1},
            )
            assert receipt.error is not None
            assert receipt.error.code == ErrorCode.STALE_FRAME
            assert receipt.error.details["ttl_seconds"] == 0.05

    async def test_a_frame_from_another_instance_is_refused(self, harness_factory) -> None:
        async with harness_factory() as h:
            frame, _ = await h.controller.observe()
            h.view.instance_id = "inst_2"
            receipt, _ = await h.controller.act(
                request_id="r1",
                frame_id=frame.frame_id,
                action_payload={"type": "move", "x": 1, "y": 1},
            )
            assert receipt.error is not None
            assert receipt.error.code == ErrorCode.STALE_FRAME

    async def test_a_frame_from_before_a_reboot_is_refused(self, harness_factory) -> None:
        async with harness_factory() as h:
            frame, _ = await h.controller.observe()
            h.view.boot_epoch += 1
            receipt, _ = await h.controller.act(
                request_id="r1",
                frame_id=frame.frame_id,
                action_payload={"type": "move", "x": 1, "y": 1},
            )
            assert receipt.error is not None
            assert receipt.error.code == ErrorCode.STALE_FRAME

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

    async def test_a_frame_retired_by_a_resize_says_why(self, harness_factory) -> None:
        async with harness_factory() as h:
            frame, _ = await h.controller.observe()
            h.capture.width, h.capture.height = 400, 300
            await h.controller.observe()
            receipt, _ = await h.controller.act(
                request_id="r1",
                frame_id=frame.frame_id,
                action_payload={"type": "move", "x": 1, "y": 1},
            )
            assert receipt.error is not None
            assert receipt.error.code == ErrorCode.STALE_FRAME
            assert "resolution" in receipt.error.message
            assert h.qmp.events() == []

    async def test_a_resize_is_caught_before_any_input_is_sent(self, harness_factory) -> None:
        """QMP has no resize notification, so the only way to notice that the guest changed
        resolution is to look. A coordinate action therefore refreshes the size first, and
        refuses rather than clicking into a surface that has moved underneath it."""
        capture = StubCaptureBackend()
        overrides = {"capture": CaptureConfig(max_long_edge=320, size_check_interval_seconds=0.001)}
        async with harness_factory(capture=capture, profile_overrides=overrides) as h:
            frame, _ = await h.controller.observe()
            capture.width, capture.height = 400, 300
            await asyncio.sleep(0.01)  # let the size-check interval elapse
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
            # The refresh happened, but the click did not.
            assert h.qmp.events() == []
            assert h.view.display_epoch == 2

    async def test_an_unchanged_size_does_not_block_the_action(self, harness_factory) -> None:
        capture = StubCaptureBackend()
        overrides = {"capture": CaptureConfig(max_long_edge=320, size_check_interval_seconds=0.001)}
        async with harness_factory(capture=capture, profile_overrides=overrides) as h:
            frame, _ = await h.controller.observe()
            await asyncio.sleep(0.01)
            receipt, _ = await h.controller.act(
                request_id="r1",
                frame_id=frame.frame_id,
                action_payload={"type": "move", "x": 10, "y": 10},
            )
            assert receipt.ok is True
            assert h.view.display_epoch == 1

    async def test_out_of_range_coordinates_are_refused_without_sending_input(
        self, harness_factory
    ) -> None:
        async with harness_factory() as h:
            frame, _ = await h.controller.observe()
            receipt, _ = await h.controller.act(
                request_id="r1",
                frame_id=frame.frame_id,
                action_payload={"type": "move", "x": 4000, "y": 1},
            )
            assert receipt.error is not None
            assert receipt.error.code == ErrorCode.INVALID_COORDINATES
            assert h.qmp.events() == []


class TestPointerBatching:
    async def test_a_click_addresses_the_display_device_and_presses_then_releases(
        self, harness_factory
    ) -> None:
        async with harness_factory() as h:
            frame, _ = await h.controller.observe()
            receipt, image = await h.controller.act(
                request_id="r1",
                frame_id=frame.frame_id,
                action_payload={"type": "click", "x": 160, "y": 100, "button": "left", "count": 1},
            )
            assert receipt.ok is True
            assert receipt.effect_state == "submitted"
            assert receipt.input_submitted is True
            assert receipt.action_seq == 1
            assert receipt.backend == "qmp-pointer"
            assert image is not None and image.startswith(b"\x89PNG")

            batches = h.qmp.events()
            # One phase per message: the press and the release are never merged.
            assert len(batches) == 3
            assert batches[1] == [{"type": "btn", "data": {"button": "left", "down": True}}]
            assert batches[2] == [{"type": "btn", "data": {"button": "left", "down": False}}]
            # The QMP device argument is the *display* id, not the pointer's.
            for command, arguments in h.qmp.calls:
                if command == "input-send-event":
                    assert arguments["device"] == "video0"
                    assert arguments["head"] == 0

    async def test_absolute_coordinates_travel_as_a_pair(self, harness_factory) -> None:
        async with harness_factory() as h:
            frame, _ = await h.controller.observe()
            await h.controller.act(
                request_id="r1",
                frame_id=frame.frame_id,
                action_payload={"type": "move", "x": 0, "y": 0},
            )
            assert h.qmp.absolute() == [(0, 0)]
            assert h.qmp.events()[0] == [
                {"type": "abs", "data": {"axis": "x", "value": 0}},
                {"type": "abs", "data": {"axis": "y", "value": 0}},
            ]

    async def test_a_corner_click_maps_to_the_axis_extremes(self, harness_factory) -> None:
        async with harness_factory() as h:
            frame, _ = await h.controller.observe()
            await h.controller.act(
                request_id="r1",
                frame_id=frame.frame_id,
                action_payload={"type": "move", "x": 319, "y": 199},
            )
            assert h.qmp.absolute() == [(32767, 32767)]

    async def test_a_double_click_is_two_complete_clicks(self, harness_factory) -> None:
        async with harness_factory() as h:
            frame, _ = await h.controller.observe()
            await h.controller.act(
                request_id="r1",
                frame_id=frame.frame_id,
                action_payload={"type": "click", "x": 10, "y": 10, "count": 2},
            )
            assert h.qmp.buttons() == [
                ("left", True),
                ("left", False),
                ("left", True),
                ("left", False),
            ]

    async def test_a_right_click_uses_the_right_button(self, harness_factory) -> None:
        async with harness_factory() as h:
            frame, _ = await h.controller.observe()
            await h.controller.act(
                request_id="r1",
                frame_id=frame.frame_id,
                action_payload={"type": "click", "x": 1, "y": 1, "button": "right"},
            )
            assert h.qmp.buttons() == [("right", True), ("right", False)]

    async def test_scroll_emits_one_pulse_per_tick(self, harness_factory) -> None:
        async with harness_factory() as h:
            frame, _ = await h.controller.observe()
            await h.controller.act(
                request_id="r1",
                frame_id=frame.frame_id,
                action_payload={
                    "type": "scroll",
                    "x": 50,
                    "y": 50,
                    "direction": "down",
                    "ticks": 3,
                },
            )
            assert (
                h.qmp.buttons()
                == [
                    ("wheel-down", True),
                    ("wheel-down", False),
                ]
                * 3
            )

    async def test_scroll_does_not_enter_the_button_ledger(self, harness_factory) -> None:
        """A wheel pulse is instantaneous; treating it as a held button would make the
        cleanup path chase something that was never down."""
        async with harness_factory() as h:
            frame, _ = await h.controller.observe()
            await h.controller.act(
                request_id="r1",
                frame_id=frame.frame_id,
                action_payload={"type": "scroll", "x": 5, "y": 5, "direction": "up", "ticks": 2},
            )
            assert h.controller.ledger_snapshot()["buttons"] == []

    async def test_a_drag_presses_moves_and_releases(self, harness_factory) -> None:
        async with harness_factory() as h:
            frame, _ = await h.controller.observe()
            await h.controller.act(
                request_id="r1",
                frame_id=frame.frame_id,
                action_payload={
                    "type": "drag",
                    "points": [{"x": 10, "y": 10}, {"x": 100, "y": 60}],
                    "duration_ms": 100,
                },
            )
            buttons = h.qmp.buttons()
            assert buttons[0] == ("left", True)
            assert buttons[-1] == ("left", False)
            positions = h.qmp.absolute()
            # The path is sampled rather than teleported: a single jump then release often
            # registers as a click at the destination.
            assert len(positions) >= 4
            assert positions[0] == (0, 0) or positions[0][0] < 3000
            assert positions[-1] != positions[0]

    async def test_drag_never_holds_a_button_when_it_finishes(self, harness_factory) -> None:
        async with harness_factory() as h:
            frame, _ = await h.controller.observe()
            await h.controller.act(
                request_id="r1",
                frame_id=frame.frame_id,
                action_payload={"type": "drag", "points": [{"x": 0, "y": 0}, {"x": 20, "y": 20}]},
            )
            assert h.controller.ledger_snapshot()["buttons"] == []


class TestKeyboardBatching:
    async def test_a_chord_presses_modifiers_first_and_releases_them_last(
        self, harness_factory
    ) -> None:
        async with harness_factory() as h:
            frame, _ = await h.controller.observe()
            await h.controller.act(
                request_id="r1",
                frame_id=frame.frame_id,
                action_payload={"type": "key", "keys": ["CTRL", "A"], "hold_ms": 20},
            )
            assert h.qmp.qcodes() == [
                ("ctrl", True),
                ("a", True),
                ("a", False),
                ("ctrl", False),
            ]

    async def test_text_is_sent_character_by_character(self, harness_factory) -> None:
        async with harness_factory() as h:
            frame, _ = await h.controller.observe()
            receipt, _ = await h.controller.act(
                request_id="r1",
                frame_id=frame.frame_id,
                action_payload={"type": "type_text", "text": "Hi", "mode": "keyboard"},
            )
            assert receipt.ok is True
            assert receipt.backend == "qmp-keyboard"
            assert h.qmp.qcodes() == [
                ("shift", True),
                ("h", True),
                ("h", False),
                ("shift", False),
                ("i", True),
                ("i", False),
            ]

    async def test_unsupported_text_sends_nothing_at_all(self, harness_factory) -> None:
        async with harness_factory() as h:
            frame, _ = await h.controller.observe()
            receipt, image = await h.controller.act(
                request_id="r1",
                frame_id=frame.frame_id,
                action_payload={"type": "type_text", "text": "ok你好", "mode": "keyboard"},
            )
            assert receipt.ok is False
            assert receipt.error is not None
            assert receipt.error.code == ErrorCode.UNSUPPORTED_TEXT
            assert receipt.effect_state == "not_sent"
            assert receipt.input_submitted is False
            # The whole string is validated up front, so not even the "ok" prefix is typed.
            assert h.qmp.events() == []
            assert image is None

    async def test_control_characters_must_be_explicit_key_actions(self, harness_factory) -> None:
        async with harness_factory() as h:
            frame, _ = await h.controller.observe()
            receipt, _ = await h.controller.act(
                request_id="r1",
                frame_id=frame.frame_id,
                action_payload={"type": "type_text", "text": "save\n", "mode": "keyboard"},
            )
            assert receipt.error is not None
            assert receipt.error.code == ErrorCode.UNSUPPORTED_TEXT
            assert h.qmp.events() == []


class TestIdempotency:
    async def test_a_repeated_request_returns_the_original_receipt(self, harness_factory) -> None:
        async with harness_factory() as h:
            frame, _ = await h.controller.observe()
            payload = {"type": "click", "x": 30, "y": 30}
            first, _ = await h.controller.act(
                request_id="same", frame_id=frame.frame_id, action_payload=payload
            )
            batches_after_first = len(h.qmp.events())
            second, image = await h.controller.act(
                request_id="same", frame_id=frame.frame_id, action_payload=payload
            )
            assert first.ok is True
            assert second.deduplicated is True
            assert second.action_seq == first.action_seq
            # Crucially: the de-duplicated replay performed no input at all.
            assert len(h.qmp.events()) == batches_after_first
            assert image is None

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
        """A retry of a successful action must return the original receipt, not a
        STALE_FRAME error: the frame is older now precisely because the action worked."""
        async with harness_factory() as h:
            frame, _ = await h.controller.observe()
            payload = {"type": "move", "x": 1, "y": 1}
            await h.controller.act(request_id="r1", frame_id=frame.frame_id, action_payload=payload)
            replay, _ = await h.controller.act(
                request_id="r1", frame_id=frame.frame_id, action_payload=payload
            )
            assert replay.ok is True
            assert replay.deduplicated is True

    async def test_a_concurrent_duplicate_waits_for_the_first_result(self, harness_factory) -> None:
        async with harness_factory() as h:
            frame, _ = await h.controller.observe()
            payload = {"type": "move", "x": 2, "y": 2}
            first, second = await asyncio.gather(
                h.controller.act(request_id="dup", frame_id=frame.frame_id, action_payload=payload),
                h.controller.act(request_id="dup", frame_id=frame.frame_id, action_payload=payload),
            )
            assert first[0].action_seq == second[0].action_seq
            assert len(h.qmp.events()) == 1


class TestEffectStates:
    async def test_a_failure_before_any_input_is_not_sent(self, harness_factory) -> None:
        qmp = StubQmp()
        qmp.failures["input-send-event"] = PaletteError(ErrorCode.QMP_COMMAND_FAILED, "no")
        qmp.fail_after["input-send-event"] = 0
        async with harness_factory(qmp=qmp) as h:
            frame, _ = await h.controller.observe()
            receipt, _ = await h.controller.act(
                request_id="r1",
                frame_id=frame.frame_id,
                action_payload={"type": "click", "x": 1, "y": 1},
            )
            assert receipt.effect_state == "not_sent"
            assert receipt.input_submitted is False
            assert receipt.ok is False

    async def test_a_failure_midway_is_partial(self, harness_factory) -> None:
        qmp = StubQmp()
        qmp.failures["input-send-event"] = PaletteError(ErrorCode.QMP_COMMAND_FAILED, "pipe died")
        qmp.fail_after["input-send-event"] = 2  # the pointer moved and the press went out
        async with harness_factory(qmp=qmp) as h:
            frame, _ = await h.controller.observe()
            receipt, _ = await h.controller.act(
                request_id="r1",
                frame_id=frame.frame_id,
                action_payload={"type": "click", "x": 1, "y": 1},
            )
            assert receipt.effect_state == "partial"
            assert receipt.input_submitted is True
            # The press was sent, so the button is remembered as possibly held.
            assert h.controller.ledger_snapshot()["buttons"] == ["left"]

    async def test_a_disconnect_after_partial_input_is_unknown(self, harness_factory) -> None:
        qmp = StubQmp()
        qmp.failures["input-send-event"] = PaletteError(
            ErrorCode.QMP_UNAVAILABLE, "the QMP channel closed"
        )
        qmp.fail_after["input-send-event"] = 1
        async with harness_factory(qmp=qmp) as h:
            frame, _ = await h.controller.observe()
            receipt, _ = await h.controller.act(
                request_id="r1",
                frame_id=frame.frame_id,
                action_payload={"type": "click", "x": 1, "y": 1},
            )
            # Input went out and the channel then failed: nobody can say whether the
            # click landed, so the receipt must not claim it did or did not.
            assert receipt.effect_state == "unknown"
            assert receipt.error is not None
            assert receipt.error.code == ErrorCode.QMP_UNAVAILABLE

    async def test_a_failed_observation_after_input_still_reports_the_input(
        self, harness_factory
    ) -> None:
        capture = StubCaptureBackend()
        async with harness_factory(capture=capture) as h:
            frame, _ = await h.controller.observe()
            capture.fail_with = PaletteError(ErrorCode.CAPTURE_FAILED, "the display went away")
            receipt, image = await h.controller.act(
                request_id="r1",
                frame_id=frame.frame_id,
                action_payload={"type": "move", "x": 3, "y": 3},
            )
            assert receipt.ok is False
            assert receipt.effect_state == "submitted"
            assert receipt.input_submitted is True
            assert receipt.observation == "failed"
            assert receipt.frame is None
            assert image is None
            assert receipt.frame_observation_required is True
            assert receipt.error is not None
            assert receipt.error.code == ErrorCode.CAPTURE_FAILED_AFTER_INPUT
            # The action sequence still advanced: the desktop may well have changed.
            assert receipt.action_seq == 1

    async def test_a_timeout_is_reported_as_an_unknown_outcome(self, harness_factory) -> None:
        capture = BlockingCapture()
        overrides = {
            "resources": ResourcesConfig(
                vcpus=1, memory_mib=512, process_memory_limit_mib=None, max_action_seconds=0.15
            )
        }
        async with harness_factory(capture=capture, profile_overrides=overrides) as h:
            frame, _ = await h.controller.observe()
            capture.hold()
            with pytest.raises(PaletteError) as info:
                await h.controller.act(
                    request_id="r1",
                    frame_id=frame.frame_id,
                    action_payload={"type": "move", "x": 1, "y": 1},
                )
            assert info.value.code is ErrorCode.ACTION_OUTCOME_UNKNOWN
            # The pointer really did move before the capture stalled, which is exactly why
            # the outcome is "unknown" rather than "nothing happened". Image pixel (1, 1)
            # of a 320x200 frame maps to round(1*32767/319)=103, round(1*32767/199)=165.
            assert h.qmp.absolute() == [(103, 165)]
            capture.release()


class TestLedgerAndRelease:
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

    async def test_a_release_that_cannot_be_confirmed_says_so(self, harness_factory) -> None:
        qmp = StubQmp()
        qmp.failures["input-send-event"] = PaletteError(
            ErrorCode.QMP_UNAVAILABLE, "the channel is gone"
        )
        qmp.fail_after["input-send-event"] = 2
        async with harness_factory(qmp=qmp) as h:
            frame, _ = await h.controller.observe()
            await h.controller.act(
                request_id="r1",
                frame_id=frame.frame_id,
                action_payload={"type": "click", "x": 1, "y": 1},
            )
            result = await h.controller.release_inputs()
            # Claiming a successful release here would be the exact lie the plan forbids.
            assert result.ok is False
            assert result.release_confirmed is False
            assert "could not be confirmed" in (result.note or "") or "unknown" in (
                result.note or ""
            )
            assert "left" in result.released_buttons
            assert h.controller.ledger_snapshot()["buttons"] == ["left"]

    async def test_releasing_with_nothing_held_is_a_no_op(self, harness_factory) -> None:
        async with harness_factory() as h:
            result = await h.controller.release_inputs()
            assert result.ok is True
            assert result.release_confirmed is True
            assert result.released_keys == []
            qmp_calls_after = len(h.qmp.calls)
            assert qmp_calls_after == 0

    async def test_emergency_release_cancels_a_running_action_and_lets_go(
        self, harness_factory
    ) -> None:
        capture = BlockingCapture()
        async with harness_factory(capture=capture) as h:
            frame, _ = await h.controller.observe()
            capture.hold()
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
            # The click had already pressed the button, so the release must have undone it.
            assert ("left", False) in h.qmp.buttons()
            task.cancel()
            with pytest.raises((asyncio.CancelledError, PaletteError)):
                await task
            # Nothing may be left held after a cancellation path.
            assert h.controller.ledger_snapshot() == {"keys": [], "buttons": []}
            capture.release()


class TestScheduling:
    async def test_actions_are_serialised(self, harness_factory) -> None:
        async with harness_factory() as h:
            frame, _ = await h.controller.observe()
            first, second = await asyncio.gather(
                h.controller.act(
                    request_id="a",
                    frame_id=frame.frame_id,
                    action_payload={"type": "key", "keys": ["TAB"]},
                ),
                h.controller.act(
                    request_id="b",
                    frame_id=frame.frame_id,
                    action_payload={"type": "key", "keys": ["ESC"]},
                ),
            )
            # Both name the same frame. They run one after the other, so the first advances
            # the action sequence and fences the second: exactly one action happened.
            outcomes = sorted([first[0].ok, second[0].ok])
            assert outcomes == [False, True]
            rejected = first[0] if not first[0].ok else second[0]
            assert rejected.error is not None
            assert rejected.error.code == ErrorCode.STALE_FRAME
            assert len(h.qmp.events()) == 2  # one chord only: one down, one up

    async def test_observations_are_queued_behind_a_running_action(self, harness_factory) -> None:
        capture = BlockingCapture()
        async with harness_factory(capture=capture) as h:
            frame, _ = await h.controller.observe()
            capture.hold()
            blocker = asyncio.create_task(h.controller.observe())
            await capture.entered.wait()
            action = asyncio.create_task(
                h.controller.act(
                    request_id="r1",
                    frame_id=frame.frame_id,
                    action_payload={"type": "move", "x": 1, "y": 1},
                )
            )
            await asyncio.sleep(0.05)
            # The action cannot jump ahead of the observation, so no input has gone out.
            assert h.qmp.events() == []
            capture.release()
            await blocker
            receipt, _ = await action
            assert receipt.ok is True
            assert receipt.action_seq == 1

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

    async def test_a_request_that_waits_too_long_for_the_queue_gives_up(
        self, harness_factory
    ) -> None:
        capture = BlockingCapture()
        overrides = {
            "resources": ResourcesConfig(
                vcpus=1,
                memory_mib=512,
                process_memory_limit_mib=None,
                max_queue_depth=8,
                max_queued_wait_seconds=0.1,
            )
        }
        async with harness_factory(capture=capture, profile_overrides=overrides) as h:
            capture.hold()
            blocker = asyncio.create_task(h.controller.observe())
            await capture.entered.wait()
            with pytest.raises(PaletteError) as info:
                await h.controller.observe()
            assert info.value.code is ErrorCode.BUSY
            assert info.value.details["queued_seconds"] == 0.1
            capture.release()
            await blocker


class TestWait:
    async def test_delay_waits_and_returns_a_frame(self, harness_factory) -> None:
        async with harness_factory() as h:
            result, image = await h.controller.wait(
                after_frame_id=None, mode="delay", timeout_ms=50, delay_ms=20
            )
            assert result.reason == "delay_elapsed"
            assert result.frame is not None
            assert image is not None

    async def test_changed_detects_a_difference(self, harness_factory) -> None:
        capture = StubCaptureBackend()
        async with harness_factory(capture=capture) as h:
            frame, _ = await h.controller.observe()
            capture.colour = (10, 10, 10)
            result, _ = await h.controller.wait(
                after_frame_id=frame.frame_id, mode="changed", timeout_ms=1500, threshold=0.001
            )
            assert result.reason == "content_changed"
            assert result.ok is True
            assert result.samples >= 1
            assert result.changed_ratio is not None and result.changed_ratio > 0.001

    async def test_changed_compares_against_the_frame_the_agent_saw(self, harness_factory) -> None:
        """The comparison baseline is the referenced frame, not a sample taken after the
        wait began; otherwise a change that already happened could never be seen."""
        capture = StubCaptureBackend()
        async with harness_factory(capture=capture) as h:
            frame, _ = await h.controller.observe()
            # Change the screen *without* taking another frame first.
            capture.colour = (250, 250, 250)
            result, _ = await h.controller.wait(
                after_frame_id=frame.frame_id, mode="changed", timeout_ms=1500, threshold=0.001
            )
            assert result.reason == "content_changed"

    async def test_changed_times_out_when_nothing_moves(self, harness_factory) -> None:
        async with harness_factory() as h:
            frame, _ = await h.controller.observe()
            result, image = await h.controller.wait(
                after_frame_id=frame.frame_id, mode="changed", timeout_ms=600, threshold=0.02
            )
            assert result.reason == "timeout"
            assert result.frame is not None
            assert image is not None

    async def test_stable_reports_when_the_screen_stops_moving(self, harness_factory) -> None:
        async with harness_factory() as h:
            frame, _ = await h.controller.observe()
            result, _ = await h.controller.wait(
                after_frame_id=frame.frame_id, mode="stable", timeout_ms=1500, threshold=0.02
            )
            assert result.reason == "content_stable"

    async def test_waiting_does_not_change_the_action_sequence(self, harness_factory) -> None:
        async with harness_factory() as h:
            frame, _ = await h.controller.observe()
            await h.controller.wait(after_frame_id=frame.frame_id, mode="delay", timeout_ms=30)
            assert h.view.action_seq == 0
            # The original frame is therefore still usable.
            receipt, _ = await h.controller.act(
                request_id="r1",
                frame_id=frame.frame_id,
                action_payload={"type": "move", "x": 1, "y": 1},
            )
            assert receipt.ok is True


class TestRegistryUnit:
    def test_an_unknown_identifier_is_not_found(self, tmp_path: Path) -> None:
        registry = FrameRegistry()
        with pytest.raises(PaletteError) as info:
            registry.get("frame_x")
        assert info.value.code is ErrorCode.FRAME_NOT_FOUND

    def test_a_retired_identifier_explains_itself(self, tmp_path: Path) -> None:
        registry = FrameRegistry()
        frame = _frame("frame_1")
        registry.add(frame)
        registry.clear(reason="the display resolution changed")
        with pytest.raises(PaletteError) as info:
            registry.get("frame_1")
        assert info.value.code is ErrorCode.STALE_FRAME
        assert info.value.details["reason"] == "the display resolution changed"

    def test_the_registry_is_bounded(self) -> None:
        registry = FrameRegistry(max_entries=3)
        for index in range(10):
            registry.add(_frame(f"frame_{index}"))
        with pytest.raises(PaletteError) as info:
            registry.get("frame_0")
        assert info.value.code is ErrorCode.FRAME_NOT_FOUND  # evicted, not retired


def _frame(frame_id: str) -> Frame:
    from datetime import datetime

    from palette.models import Viewport

    return Frame(
        frame_id=frame_id,
        vm_id="desktop",
        instance_id="inst_1",
        boot_epoch=1,
        display_id="video0",
        head=0,
        display_epoch=1,
        controller_epoch=1,
        action_seq=0,
        native_size=Size(width=320, height=200),
        image_size=Size(width=320, height=200),
        viewport=Viewport(x=0, y=0, width=320, height=200),
        transform_id="full_320x200",
        captured_at=datetime.now().astimezone(),
    )
