"""Run ownership is enforced in the service and at queued action execution."""

from __future__ import annotations

import asyncio
import time

import pytest

from palette.errors import ErrorCode, PaletteError
from palette.input.scheduler import JobKind
from palette.service import PaletteService
from palette.vm.manager import VmInstance


async def test_claim_excludes_legacy_writers_but_allows_observation(
    service: PaletteService, running_vm: VmInstance
) -> None:
    identity = running_vm.view.instance_id
    claim = await service.driver.acquire("desktop", instance_id=identity, run_id="run1")
    with pytest.raises(PaletteError) as busy:
        service.control.require_agent("desktop")
    assert busy.value.code == ErrorCode.BUSY
    observed, _ = await service.observe_image("desktop")
    with service.driver.bind(claim["claim_token"]):
        receipt, _ = await service.act(
            "desktop",
            instance_id=identity,
            frame_id=observed.frame.frame_id,
            request_id="claim-action",
            action={"type": "click", "x": 4, "y": 4},
        )
        assert receipt.ok
        assert (await service.driver.release("desktop"))["release_confirmed"]
        with pytest.raises(PaletteError):
            service.control.require_agent("desktop")
    service.control.require_agent("desktop")


async def test_queued_unclaimed_action_cannot_execute_after_claim_acquisition(
    service: PaletteService, running_vm: VmInstance
) -> None:
    controller = service.controller_for(running_vm)
    observed, _ = await service.observe_image("desktop")
    waiting = asyncio.Event()
    started = asyncio.Event()

    async def blocker() -> None:
        started.set()
        await waiting.wait()

    block = asyncio.create_task(
        controller.scheduler.submit(
            controller.scheduler.make_job(JobKind.OBSERVE, "blocker", blocker)
        )
    )
    await started.wait()
    action = asyncio.create_task(
        service.act(
            "desktop",
            instance_id=running_vm.view.instance_id,
            frame_id=observed.frame.frame_id,
            request_id="queued-old-writer",
            action={"type": "click", "x": 4, "y": 4},
        )
    )
    await asyncio.sleep(0)
    # The claim is installed before handover invalidates the old action queue.
    acquire = asyncio.create_task(
        service.driver.acquire("desktop", instance_id=running_vm.view.instance_id, run_id="run1")
    )
    waiting.set()
    try:
        await block
    except PaletteError as error:
        assert error.code == ErrorCode.STALE_FRAME
    claim = await acquire
    result, _ = await action
    assert not result.ok
    with service.driver.bind(claim["claim_token"]):
        await service.driver.release("desktop")


async def test_takeover_and_expiry_revoke_run_tokens(
    service: PaletteService, running_vm: VmInstance
) -> None:
    identity = running_vm.view.instance_id
    claim = await service.driver.acquire("desktop", instance_id=identity, run_id="run1")
    lease = await service.control.acquire("desktop", identity)
    assert "desktop" not in service.driver.claims
    with service.driver.bind(claim["claim_token"]), pytest.raises(PaletteError):
        service.control.require_agent("desktop")
    await service.control.release("desktop", lease["lease_id"])
    second = await service.driver.acquire("desktop", instance_id=identity, run_id="run2")
    service.driver.claims["desktop"].expires = time.monotonic() - 1
    await asyncio.sleep(0.4)
    assert "desktop" not in service.driver.claims
    with service.driver.bind(second["claim_token"]), pytest.raises(PaletteError):
        service.control.require_agent("desktop")
    third = await service.driver.acquire("desktop", instance_id=identity, run_id="run3")
    await service.take_control("desktop", reason="CLI takeover")
    assert "desktop" not in service.driver.claims
    with service.driver.bind(third["claim_token"]), pytest.raises(PaletteError):
        service.control.require_agent("desktop")
