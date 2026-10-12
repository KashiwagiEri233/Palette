"""The action path end to end: service → controller → QMP → the fake guest process.

Assertions here read the fake QEMU's own record of what it received over the wire, so
they cover the full stack including the framing and the device addressing.
"""

from __future__ import annotations

import asyncio

import pytest

from conftest import read_command_log, write_behavior
from palette.errors import ErrorCode, PaletteError
from palette.service import PaletteService


def input_events(instance) -> list[dict]:
    """Every input event the fake QEMU received, in order."""
    events: list[dict] = []
    for record in read_command_log(instance):
        if record.get("command") == "input-send-event":
            events.extend(record["arguments"]["events"])
    return events


def input_batches(instance) -> list[dict]:
    return [
        record["arguments"]
        for record in read_command_log(instance)
        if record.get("command") == "input-send-event"
    ]


def screendumps(instance) -> list[dict]:
    return [
        record["arguments"]
        for record in read_command_log(instance)
        if record.get("command") == "screendump"
    ]


class TestObserve:
    async def test_an_observation_returns_an_image_and_a_frame(
        self, service: PaletteService, running_vm
    ) -> None:
        result, image = await service.observe_image("desktop")
        assert result.ok is True
        assert result.frame is not None
        assert image.startswith(b"\x89PNG")
        assert result.image_bytes == len(image)
        assert result.frame.native_size.width == 320
        assert result.frame.cursor_position_source == "not_observed"

    async def test_the_screendump_names_the_display_and_asks_for_png(
        self, service: PaletteService, running_vm
    ) -> None:
        await service.observe("desktop")
        requests = screendumps(running_vm)
        assert requests
        assert requests[-1]["device"] == "video0"
        assert requests[-1]["head"] == 0
        assert requests[-1]["format"] == "png"

    async def test_the_frame_file_does_not_outlive_the_request(
        self, service: PaletteService, running_vm
    ) -> None:
        """Screenshots are transient by default: the service must not quietly accumulate a
        record of everything the agent looked at."""
        await service.observe("desktop")
        assert list(running_vm.paths.frames.glob("*.png")) == []

    async def test_a_build_without_png_falls_back_and_still_returns_a_png(
        self, service: PaletteService, running_vm
    ) -> None:
        write_behavior(running_vm, {"png_unsupported": True})
        result, image = await service.observe_image("desktop")
        assert result.ok is True
        assert image.startswith(b"\x89PNG")
        formats = [request.get("format") for request in screendumps(running_vm)]
        assert "png" in formats and None in formats

    async def test_a_screendump_failure_is_reported_as_a_capture_failure(
        self, service: PaletteService, running_vm
    ) -> None:
        write_behavior(running_vm, {"screendump_fails": True})
        with pytest.raises(PaletteError) as info:
            await service.observe("desktop")
        assert info.value.code is ErrorCode.CAPTURE_FAILED

    async def test_an_image_size_preference_is_honoured_and_declared(
        self, service: PaletteService, running_vm
    ) -> None:
        result, image = await service.observe_image("desktop", max_long_edge=64)
        assert result.frame is not None
        assert result.frame.image_size.width == 64
        assert result.frame.native_size.width == 320
        assert result.scaled is True
        assert result.note is not None and "reduced" in result.note
        assert len(image) > 0

    async def test_an_absurd_image_size_is_refused(
        self, service: PaletteService, running_vm
    ) -> None:
        with pytest.raises(PaletteError) as info:
            await service.observe("desktop", max_long_edge=100000)
        assert info.value.code is ErrorCode.INVALID_ARGUMENT


class TestActions:
    async def test_a_click_reaches_the_guest_as_pointer_events(
        self, service: PaletteService, running_vm
    ) -> None:
        observed = await service.observe("desktop")
        assert observed.frame is not None
        before = len(input_batches(running_vm))
        receipt, image = await service.act(
            "desktop",
            instance_id=running_vm.view.instance_id,
            frame_id=observed.frame.frame_id,
            request_id="click-1",
            action={"type": "click", "x": 10, "y": 20},
        )
        assert receipt.ok is True
        assert receipt.effect_state == "submitted"
        assert receipt.source == "qmp"
        assert receipt.observation == "captured"
        assert receipt.frame is not None
        assert image is not None and image.startswith(b"\x89PNG")
        assert receipt.elapsed_ms >= 0

        batches = input_batches(running_vm)[before:]
        assert len(batches) == 3
        assert [event["type"] for event in batches[0]["events"]] == ["abs", "abs"]
        assert batches[0]["events"][0]["data"]["value"] == round(10 * 32767 / 319)
        assert batches[0]["events"][1]["data"]["value"] == round(20 * 32767 / 199)
        assert batches[1]["events"][0]["data"] == {"button": "left", "down": True}
        assert batches[2]["events"][0]["data"] == {"button": "left", "down": False}
        for batch in batches:
            assert batch["device"] == "video0"

    async def test_text_reaches_the_guest_as_key_events(
        self, service: PaletteService, running_vm
    ) -> None:
        observed = await service.observe("desktop")
        assert observed.frame is not None
        before = len(input_batches(running_vm))
        receipt, _ = await service.act(
            "desktop",
            instance_id=running_vm.view.instance_id,
            frame_id=observed.frame.frame_id,
            request_id="text-1",
            action={"type": "type_text", "text": "Hi!", "mode": "keyboard"},
        )
        assert receipt.ok is True
        assert receipt.backend == "qmp-keyboard"
        qcodes = [
            (event["data"]["key"]["data"], event["data"]["down"])
            for batch in input_batches(running_vm)[before:]
            for event in batch["events"]
        ]
        assert qcodes == [
            ("shift", True),
            ("h", True),
            ("h", False),
            ("shift", False),
            ("i", True),
            ("i", False),
            ("shift", True),
            ("1", True),
            ("1", False),
            ("shift", False),
        ]

    async def test_unsupported_text_never_reaches_the_guest(
        self, service: PaletteService, running_vm
    ) -> None:
        observed = await service.observe("desktop")
        assert observed.frame is not None
        before = len(input_batches(running_vm))
        receipt, image = await service.act(
            "desktop",
            instance_id=running_vm.view.instance_id,
            frame_id=observed.frame.frame_id,
            request_id="text-2",
            action={"type": "type_text", "text": "你好", "mode": "keyboard"},
        )
        assert receipt.ok is False
        assert receipt.error is not None
        assert receipt.error.code == ErrorCode.UNSUPPORTED_TEXT
        assert receipt.effect_state == "not_sent"
        assert image is None
        assert len(input_batches(running_vm)) == before

    async def test_a_second_action_on_a_stale_frame_is_refused(
        self, service: PaletteService, running_vm
    ) -> None:
        observed = await service.observe("desktop")
        assert observed.frame is not None
        await service.act(
            "desktop",
            instance_id=running_vm.view.instance_id,
            frame_id=observed.frame.frame_id,
            request_id="first",
            action={"type": "move", "x": 5, "y": 5},
        )
        before = len(input_batches(running_vm))
        receipt, _ = await service.act(
            "desktop",
            instance_id=running_vm.view.instance_id,
            frame_id=observed.frame.frame_id,
            request_id="second",
            action={"type": "click", "x": 5, "y": 5},
        )
        assert receipt.ok is False
        assert receipt.error is not None
        assert receipt.error.code == ErrorCode.STALE_FRAME
        # The refused action injected nothing at all.
        assert len(input_batches(running_vm)) == before

    async def test_repeating_a_request_id_does_not_click_twice(
        self, service: PaletteService, running_vm
    ) -> None:
        observed = await service.observe("desktop")
        assert observed.frame is not None
        arguments = {
            "instance_id": running_vm.view.instance_id,
            "frame_id": observed.frame.frame_id,
            "request_id": "same-id",
            "action": {"type": "click", "x": 15, "y": 15},
        }
        first, _ = await service.act("desktop", **arguments)
        before = len(input_batches(running_vm))
        second, image = await service.act("desktop", **arguments)
        assert first.ok is True
        assert second.deduplicated is True
        assert second.action_seq == first.action_seq
        assert len(input_batches(running_vm)) == before
        assert image is None

    async def test_a_different_request_id_is_a_new_action(
        self, service: PaletteService, running_vm
    ) -> None:
        observed = await service.observe("desktop")
        assert observed.frame is not None
        first, _ = await service.act(
            "desktop",
            instance_id=running_vm.view.instance_id,
            frame_id=observed.frame.frame_id,
            request_id="one",
            action={"type": "move", "x": 5, "y": 5},
        )
        second_frame = first.frame
        assert second_frame is not None
        second, _ = await service.act(
            "desktop",
            instance_id=running_vm.view.instance_id,
            frame_id=second_frame.frame_id,
            request_id="two",
            action={"type": "move", "x": 6, "y": 6},
        )
        assert second.ok is True
        assert second.action_seq == first.action_seq + 1

    async def test_an_instance_mismatch_is_refused(
        self, service: PaletteService, running_vm
    ) -> None:
        observed = await service.observe("desktop")
        assert observed.frame is not None
        with pytest.raises(PaletteError) as info:
            await service.act(
                "desktop",
                instance_id="inst_other",
                frame_id=observed.frame.frame_id,
                request_id="r",
                action={"type": "move", "x": 1, "y": 1},
            )
        assert info.value.code is ErrorCode.STALE_FRAME

    async def test_a_reboot_invalidates_an_old_frame(
        self, service: PaletteService, running_vm
    ) -> None:
        observed = await service.observe("desktop")
        assert observed.frame is not None
        await service.manager.reset("desktop")
        # The instance identifier alone is enough to refuse: the frame names a process that
        # no longer exists, and the service never reinterprets it against its replacement.
        with pytest.raises(PaletteError) as info:
            await service.act(
                "desktop",
                instance_id=observed.frame.instance_id,
                frame_id=observed.frame.frame_id,
                request_id="r",
                action={"type": "move", "x": 1, "y": 1},
            )
        assert info.value.code is ErrorCode.STALE_FRAME
        assert info.value.details["expected_instance_id"] != observed.frame.instance_id

    async def test_a_keyboard_chord_is_sent_in_press_release_order(
        self, service: PaletteService, running_vm
    ) -> None:
        observed = await service.observe("desktop")
        assert observed.frame is not None
        before = len(input_batches(running_vm))
        await service.act(
            "desktop",
            instance_id=running_vm.view.instance_id,
            frame_id=observed.frame.frame_id,
            request_id="chord",
            action={"type": "key", "keys": ["CTRL", "V"], "hold_ms": 20},
        )
        qcodes = [
            (event["data"]["key"]["data"], event["data"]["down"])
            for batch in input_batches(running_vm)[before:]
            for event in batch["events"]
        ]
        assert qcodes == [("ctrl", True), ("v", True), ("v", False), ("ctrl", False)]

    async def test_a_drag_releases_the_button_at_the_end(
        self, service: PaletteService, running_vm
    ) -> None:
        observed = await service.observe("desktop")
        assert observed.frame is not None
        receipt, _ = await service.act(
            "desktop",
            instance_id=running_vm.view.instance_id,
            frame_id=observed.frame.frame_id,
            request_id="drag",
            action={
                "type": "drag",
                "points": [{"x": 5, "y": 5}, {"x": 50, "y": 40}],
                "duration_ms": 60,
            },
        )
        assert receipt.ok is True
        buttons = [
            (event["data"]["button"], event["data"]["down"])
            for event in input_events(running_vm)
            if event["type"] == "btn"
        ]
        assert buttons[0] == ("left", True)
        assert buttons[-1] == ("left", False)
        assert buttons.count(("left", True)) == 1
        assert service.manager.get("desktop").controller.ledger_snapshot()["buttons"] == []


class TestDisplayChange:
    async def test_a_resolution_change_retires_earlier_frames(
        self, service: PaletteService, running_vm
    ) -> None:
        observed = await service.observe("desktop")
        assert observed.frame is not None
        epoch = running_vm.view.display_epoch
        write_behavior(running_vm, {"width": 400, "height": 300})
        later = await service.observe("desktop")
        assert later.frame is not None
        assert later.frame.display_epoch == epoch + 1
        assert later.frame.native_size.width == 400

        receipt, _ = await service.act(
            "desktop",
            instance_id=running_vm.view.instance_id,
            frame_id=observed.frame.frame_id,
            request_id="after-resize",
            action={"type": "click", "x": 10, "y": 10},
        )
        assert receipt.ok is False
        assert receipt.error is not None
        assert receipt.error.code == ErrorCode.STALE_FRAME

    async def test_actions_keep_working_after_the_guest_changes_resolution(
        self, service: PaletteService, running_vm
    ) -> None:
        """The service-level twin of the controller regression: a resize must retire the old
        frames without bricking the instance. The status a caller reads must agree with the
        frames they are being handed."""
        await service.observe("desktop")
        write_behavior(running_vm, {"width": 400, "height": 300})
        later = await service.observe("desktop")
        assert later.frame is not None
        assert later.frame.native_size.width == 400

        receipt, image = await service.act(
            "desktop",
            instance_id=running_vm.view.instance_id,
            frame_id=later.frame.frame_id,
            request_id="after-resize-ok",
            action={"type": "click", "x": 10, "y": 10},
        )
        assert receipt.ok is True, receipt.error
        assert image is not None

        status = await service.status("desktop")
        assert status.status is not None
        assert status.status.display is not None
        assert status.status.display.width == 400
        assert status.status.display.height == 300

    async def test_a_coordinate_is_mapped_through_the_frame_that_carried_it(
        self, service: PaletteService, running_vm
    ) -> None:
        """A shrunken image changes the mapping; the action must use the transform of its
        own frame rather than the current default size."""
        small, _ = await service.observe_image("desktop", max_long_edge=64)
        assert small.frame is not None
        before = len(input_batches(running_vm))
        await service.act(
            "desktop",
            instance_id=running_vm.view.instance_id,
            frame_id=small.frame.frame_id,
            request_id="scaled",
            action={"type": "move", "x": 63, "y": 39},
        )
        batch = input_batches(running_vm)[before]
        # The last pixel of the 64x40 image maps to the last pixel of the 320x200 surface.
        assert batch["events"][0]["data"]["value"] == 32767
        assert batch["events"][1]["data"]["value"] == 32767


class TestWait:
    async def test_a_delay_wait_returns_a_fresh_frame(
        self, service: PaletteService, running_vm
    ) -> None:
        result, image = await service.wait(
            "desktop", after_frame_id=None, mode="delay", timeout_ms=50, delay_ms=10
        )
        assert result.ok is True
        assert result.reason == "delay_elapsed"
        assert result.frame is not None
        assert image is not None

    async def test_a_timeout_is_bounded_and_reported(
        self, service: PaletteService, running_vm
    ) -> None:
        observed = await service.observe("desktop")
        assert observed.frame is not None
        result, _ = await service.wait(
            "desktop",
            after_frame_id=observed.frame.frame_id,
            mode="changed",
            timeout_ms=400,
            threshold=0.05,
        )
        assert result.reason == "timeout"
        assert result.frame is not None

    async def test_changed_requires_a_reference_frame(
        self, service: PaletteService, running_vm
    ) -> None:
        with pytest.raises(PaletteError) as info:
            await service.wait("desktop", after_frame_id=None, mode="changed", timeout_ms=100)
        assert info.value.code is ErrorCode.INVALID_ARGUMENT

    async def test_an_unsupported_mode_is_refused(
        self, service: PaletteService, running_vm
    ) -> None:
        with pytest.raises(PaletteError) as info:
            await service.wait("desktop", after_frame_id=None, mode="teleport", timeout_ms=100)
        assert info.value.code is ErrorCode.UNSUPPORTED_CAPABILITY

    async def test_the_wait_budget_is_capped_at_ten_seconds(
        self, service: PaletteService, running_vm
    ) -> None:
        with pytest.raises(PaletteError) as info:
            await service.wait("desktop", after_frame_id=None, mode="delay", timeout_ms=60000)
        assert info.value.code is ErrorCode.INVALID_ARGUMENT

    async def test_waiting_does_not_consume_the_frame(
        self, service: PaletteService, running_vm
    ) -> None:
        observed = await service.observe("desktop")
        assert observed.frame is not None
        await service.wait("desktop", after_frame_id=None, mode="delay", timeout_ms=60, delay_ms=20)
        receipt, _ = await service.act(
            "desktop",
            instance_id=running_vm.view.instance_id,
            frame_id=observed.frame.frame_id,
            request_id="after-wait",
            action={"type": "move", "x": 1, "y": 1},
        )
        assert receipt.ok is True


class TestRelease:
    async def test_releasing_a_clean_vm_is_a_no_op(
        self, service: PaletteService, running_vm
    ) -> None:
        result = await service.release_inputs("desktop", instance_id=running_vm.view.instance_id)
        assert result.ok is True
        assert result.release_confirmed is True
        assert result.released_buttons == []

    async def test_releasing_an_instance_that_is_gone_is_refused(
        self, service: PaletteService, running_vm
    ) -> None:
        with pytest.raises(PaletteError) as info:
            await service.release_inputs("desktop", instance_id="inst_gone")
        assert info.value.code is ErrorCode.STALE_FRAME


class TestAuthorization:
    async def test_an_agent_may_start_but_not_reset(self, service: PaletteService) -> None:
        """Starting is open to callers in this profile; the destructive operations are not.
        The two are separate grants precisely so a profile can have one without the other."""
        started = await service.start("desktop", actor="agent")
        assert started.ok is True
        await service.manager.stop("desktop", force=True)
        with pytest.raises(PaletteError) as info:
            await service.reset("desktop", actor="agent")
        assert info.value.code is ErrorCode.POLICY_DENIED

    async def test_a_profile_can_close_starting_again(
        self, service: PaletteService, base_image
    ) -> None:
        from palette.config import LifecyclePolicy
        from support.profiles import make_profile

        closed = make_profile(base_image, lifecycle=LifecyclePolicy(allow_agent_start=False))
        service.config.profiles["default"] = closed
        with pytest.raises(PaletteError) as info:
            await service.start("desktop", actor="agent")
        assert info.value.code is ErrorCode.POLICY_DENIED
        assert info.value.details["operation"] == "start"

    async def test_the_shipped_profile_does_not_let_an_agent_reset_a_vm(
        self, service: PaletteService, running_vm
    ) -> None:
        with pytest.raises(PaletteError) as info:
            await service.reset("desktop", actor="agent")
        assert info.value.code is ErrorCode.POLICY_DENIED

    async def test_an_operator_may_do_both(self, service: PaletteService) -> None:
        result = await service.start("desktop", actor="operator")
        assert result.ok is True
        reset = await service.reset("desktop", actor="operator")
        assert reset.ok is True
        assert reset.instance_id != result.instance_id

    async def test_guest_tools_are_refused_when_the_profile_disables_them(
        self, service: PaletteService, running_vm
    ) -> None:
        with pytest.raises(PaletteError) as info:
            await service.guest_input_text(
                "desktop",
                instance_id=running_vm.view.instance_id,
                boot_epoch=running_vm.view.boot_epoch,
                frame_id=None,
                request_id="guest-1",
                text="hi",
                method="native_text",
            )
        assert info.value.code is ErrorCode.GUEST_TOOL_UNAVAILABLE

    async def test_an_unregistered_application_is_refused(
        self, service: PaletteService, running_vm
    ) -> None:
        with pytest.raises(PaletteError) as info:
            await service.guest_launch_app(
                "desktop",
                instance_id=running_vm.view.instance_id,
                boot_epoch=running_vm.view.boot_epoch,
                request_id="launch-1",
                app_id="bash",
                options={},
            )
        assert info.value.code in {
            ErrorCode.GUEST_TOOL_UNAVAILABLE,
            ErrorCode.UNSUPPORTED_CAPABILITY,
        }

    async def test_control_handover_bumps_the_epoch_and_retires_frames(
        self, service: PaletteService, running_vm
    ) -> None:
        observed = await service.observe("desktop")
        assert observed.frame is not None
        epoch = running_vm.view.controller_epoch
        new_epoch = await service.take_control("desktop", reason="human takeover")
        assert new_epoch == epoch + 1
        receipt, _ = await service.act(
            "desktop",
            instance_id=running_vm.view.instance_id,
            frame_id=observed.frame.frame_id,
            request_id="after-takeover",
            action={"type": "move", "x": 1, "y": 1},
        )
        assert receipt.ok is False
        assert receipt.error is not None
        assert receipt.error.code in {ErrorCode.CONTROL_NOT_OWNED, ErrorCode.STALE_FRAME}


class TestStatus:
    async def test_status_is_honest_about_what_is_ready(
        self, service: PaletteService, running_vm
    ) -> None:
        status = await service.status("desktop")
        assert status.ok is True
        assert status.status is not None
        assert status.status.state == "RUNNING"
        assert status.status.qmp_ready is True
        assert status.status.screen_ready is True
        # Never inferred from query-status.
        assert status.status.desktop_ready is None

    async def test_status_reports_the_display_and_the_capabilities(
        self, service: PaletteService, running_vm
    ) -> None:
        status = await service.status("desktop")
        assert status.status is not None
        assert status.status.display is not None
        assert status.status.display.width == 320
        assert "observe" in status.capabilities
        assert "control" in status.capabilities
        # The shipped profile grants agents no lifecycle authority.
        assert "lifecycle.reset" not in status.capabilities
        assert status.status.guest_tools.enabled is False

    async def test_status_for_an_unknown_vm_is_a_business_failure(
        self, service: PaletteService
    ) -> None:
        status = await service.status("ghost")
        assert status.ok is False
        assert status.error is not None
        assert status.error.code == ErrorCode.VM_NOT_FOUND

    async def test_status_stops_being_running_once_qmp_is_gone(
        self, service: PaletteService, running_vm
    ) -> None:
        """A guest whose management channel died must not be advertised as usable."""
        running_vm.process.kill()
        for _ in range(100):
            if running_vm.state.value == "FAILED":
                break
            await asyncio.sleep(0.02)
        assert running_vm.state.value == "FAILED"
        assert running_vm.view.qmp_ready is False
        with pytest.raises(PaletteError) as info:
            await service.observe("desktop")
        assert info.value.code is ErrorCode.VM_NOT_RUNNING
