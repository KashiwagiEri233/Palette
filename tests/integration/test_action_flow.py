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

    async def test_a_build_without_png_falls_back_and_still_returns_a_png(
        self, service: PaletteService, running_vm
    ) -> None:
        write_behavior(running_vm, {"png_unsupported": True})
        result, image = await service.observe_image("desktop")
        assert result.ok is True
        assert image.startswith(b"\x89PNG")
        formats = [request.get("format") for request in screendumps(running_vm)]
        assert "png" in formats and None in formats

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


class TestAuthorization:
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
