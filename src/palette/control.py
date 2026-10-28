"""Operator leases and real-time input, sharing the transaction controller."""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from .cleanup import close_all, join_cleanup
from .errors import ErrorCode, PaletteError
from .input.keyboard import QCODES
from .operations import VmOperations
from .timing import TimingWindow

if TYPE_CHECKING:
    from .service import PaletteService


class EventModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Pointer(EventModel):
    type: Literal["move"]
    x: int = Field(ge=0, le=8192)
    y: int = Field(ge=0, le=8192)


class Button(EventModel):
    type: Literal["button"]
    button: Literal["left", "middle", "right"]
    down: bool


class Key(EventModel):
    type: Literal["key"]
    key: str = Field(min_length=1, max_length=32)
    down: bool


class Wheel(EventModel):
    type: Literal["wheel"]
    direction: Literal["up", "down", "left", "right"]
    steps: int = Field(ge=1, le=8)


EVENTS = TypeAdapter(list[Annotated[Pointer | Button | Key | Wheel, Field(discriminator="type")]])


@dataclass
class Lease:
    vm_id: str
    instance_id: str
    boot_epoch: int
    controller_epoch: int
    token: str = field(default_factory=lambda: secrets.token_hex(24))
    expires: float = field(default_factory=lambda: time.monotonic() + 3)
    state: Literal["held", "releasing", "faulted"] = "held"
    acquiring: bool = False
    seq: int = 0
    receipts: OrderedDict[int, tuple[str, dict[str, Any]]] = field(default_factory=OrderedDict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ControlManager:
    def __init__(self, service: PaletteService) -> None:
        self.service = service
        self.leases: dict[str, Lease] = {}
        self._operations = VmOperations()
        self._watcher: asyncio.Task[None] | None = None
        self._revocations: dict[str, asyncio.Task[None]] = {}
        self.timings = TimingWindow()

    def start(self) -> None:
        if self._watcher is None or self._watcher.done():
            self._watcher = asyncio.create_task(self._watch(), name="control-leases")

    async def close(self) -> None:
        if self._watcher is not None:
            self._watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watcher
        if self._revocations:
            await asyncio.gather(*list(self._revocations.values()), return_exceptions=True)
        await close_all(partial(self.revoke, vm_id) for vm_id in list(self.leases))

    def require_agent(self, vm_id: str) -> None:
        if vm_id in self.leases:
            raise PaletteError(ErrorCode.BUSY, "this VM is under operator control")
        self.service.driver.require(vm_id)

    def validate(self, vm_id: str, token: str) -> Lease:
        lease = self.leases.get(vm_id)
        instance = self.service.manager.get(vm_id)
        if (
            lease is None
            or not secrets.compare_digest(lease.token, token)
            or lease.state != "held"
            or lease.expires <= time.monotonic()
            or lease.instance_id != instance.view.instance_id
            or lease.boot_epoch != instance.view.boot_epoch
            or lease.controller_epoch != instance.view.controller_epoch
        ):
            raise PaletteError(ErrorCode.STALE_FRAME, "the operator lease is no longer valid")
        return lease

    async def acquire(self, vm_id: str, instance_id: str) -> dict[str, Any]:
        self.service.manager.operations.require_accepting()
        async with self.service.manager.operations.hold(vm_id), self._operations.hold(vm_id):
            self.service.manager.operations.require_accepting()
            instance = await self.service.manager.require_running(vm_id)
            self.service.require_instance_id(instance, instance_id)
            if not await self.service.driver.revoke(vm_id):
                raise PaletteError(
                    ErrorCode.QMP_UNAVAILABLE, "Agent input release was not confirmed"
                )
            if vm_id in self.leases:
                raise PaletteError(ErrorCode.BUSY, "another operator holds this VM")
            lease = Lease(
                vm_id,
                instance_id,
                instance.view.boot_epoch,
                instance.view.controller_epoch,
                acquiring=True,
            )
            self.leases[vm_id] = lease
            controller = self.service.controller_for(instance)
            controller.mutation_guard = lambda: self.require_agent(vm_id)
            try:
                lease.controller_epoch = await self.service.take_control(vm_id, reason="operator")
                if any(controller.ledger_snapshot().values()):
                    lease.state = "faulted"
                    raise PaletteError(
                        ErrorCode.QMP_UNAVAILABLE, "held inputs could not be released"
                    )
                lease.expires = time.monotonic() + 3
                # The watcher must not queue revocation while takeover has advanced
                # the instance epoch but has not yet returned it to this lease.
                lease.acquiring = False
            except BaseException:
                lease.acquiring = False
                if lease.state != "faulted":
                    self.leases.pop(vm_id, None)
                raise
            return {
                "lease_id": lease.token,
                "instance_id": instance_id,
                "boot_epoch": lease.boot_epoch,
                "controller_epoch": lease.controller_epoch,
                "display_epoch": instance.view.display_epoch,
                "expires_after_ms": 3000,
            }

    def heartbeat(self, vm_id: str, token: str) -> dict[str, Any]:
        lease = self.validate(vm_id, token)
        lease.expires = time.monotonic() + 3
        return {"expires_after_ms": 3000}

    async def release(self, vm_id: str, token: str) -> dict[str, Any]:
        async with self.service.manager.operations.hold(vm_id):
            lease = self.leases.get(vm_id)
            if lease is None:
                return {"release_confirmed": True}
            if not secrets.compare_digest(lease.token, token):
                raise PaletteError(
                    ErrorCode.POLICY_DENIED, "this lease belongs to another operator"
                )
            return {"release_confirmed": await self.revoke(vm_id, expected_lease=lease)}

    async def revoke(
        self,
        vm_id: str,
        *,
        expected_lease: Lease | None = None,
        expected_instance_id: str | None = None,
    ) -> bool:
        async with self.service.manager.operations.hold(vm_id), self._operations.hold(vm_id):
            if (
                expected_lease is None
                and expected_instance_id is None
                and not await self.service.driver.revoke(vm_id)
            ):
                return False
            lease = self.leases.get(vm_id)
            if (expected_lease is not None and lease is not expected_lease) or (
                expected_instance_id is not None
                and lease is not None
                and lease.instance_id != expected_instance_id
            ):
                return True
            return await join_cleanup(asyncio.create_task(self._revoke_locked(vm_id)))

    async def _revoke_locked(self, vm_id: str) -> bool:
        lease = self.leases.get(vm_id)
        if lease is None:
            return True
        lease.state = "releasing"
        instance = self.service.manager.find(vm_id)
        if instance is None or instance.view.instance_id != lease.instance_id:
            self.leases.pop(vm_id, None)
            return True
        instance.view.controller_epoch += 1
        controller = instance.controller
        confirmed = True
        if controller is not None:
            result = await controller.emergency_release()
            confirmed = result.release_confirmed
        if instance.qemu_guest_agent is not None:
            await instance.qemu_guest_agent.close_files()
        # Once the process has exited, it cannot retain held guest inputs even if
        # its already-closed controller could not acknowledge emergency_release.
        if instance.process is None or instance.process.returncode is not None:
            confirmed = True
        if confirmed:
            self.leases.pop(vm_id, None)
        else:
            lease.state = "faulted"
        return confirmed

    async def input(
        self,
        vm_id: str,
        token: str,
        seq: int,
        display_epoch: int,
        width: int,
        height: int,
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not 1 <= len(events) <= 64 or not 1 <= seq <= 2**53 - 1:
            raise PaletteError(ErrorCode.INVALID_ARGUMENT, "invalid input batch")
        parsed = EVENTS.validate_python(events)
        fingerprint = json.dumps([display_epoch, width, height, events], sort_keys=True)
        lease = self.validate(vm_id, token)
        async with lease.lock:
            self.validate(vm_id, token)
            previous = lease.receipts.get(seq)
            if previous is not None:
                if fingerprint != previous[0]:
                    raise PaletteError(
                        ErrorCode.REQUEST_ID_CONFLICT, "sequence has different input"
                    )
                return previous[1]
            if seq != lease.seq + 1:
                raise PaletteError(ErrorCode.INVALID_ARGUMENT, "operator sequence is out of order")
            instance = self.service.manager.get(vm_id)
            size = instance.view.native_size
            if size is None or (width, height) != (size.width, size.height):
                raise PaletteError(ErrorCode.STALE_FRAME, "the display size changed; observe again")
            if display_epoch != instance.view.display_epoch:
                raise PaletteError(ErrorCode.STALE_FRAME, "the display epoch changed")
            qmp_events: list[dict[str, Any]] = []
            for event in parsed:
                if isinstance(event, Pointer):
                    if event.x >= width or event.y >= height:
                        raise PaletteError(
                            ErrorCode.INVALID_ARGUMENT, "pointer is outside the display"
                        )
                    for axis, value, extent in (("x", event.x, width), ("y", event.y, height)):
                        qmp_events.append(
                            {
                                "type": "abs",
                                "data": {
                                    "axis": axis,
                                    "value": round(value * 32767 / max(1, extent - 1)),
                                },
                            }
                        )
                elif isinstance(event, Key):
                    if event.key not in QCODES:
                        raise PaletteError(ErrorCode.INVALID_ARGUMENT, "unsupported physical key")
                    qmp_events.append(
                        {
                            "type": "key",
                            "data": {
                                "down": event.down,
                                "key": {"type": "qcode", "data": event.key},
                            },
                        }
                    )
                elif isinstance(event, Button):
                    qmp_events.append(
                        {"type": "btn", "data": {"button": event.button, "down": event.down}}
                    )
                else:
                    for _ in range(event.steps):
                        for down in (True, False):
                            qmp_events.append(
                                {
                                    "type": "btn",
                                    "data": {"button": "wheel-" + event.direction, "down": down},
                                }
                            )
            try:

                def check() -> None:
                    self.validate(vm_id, token)
                    if (
                        instance.view.display_epoch != display_epoch
                        or instance.view.native_size != size
                    ):
                        raise PaletteError(
                            ErrorCode.STALE_FRAME, "the display changed during input"
                        )

                action_seq = await self.service.controller_for(instance).operator_input(
                    qmp_events, check
                )
            except BaseException:
                lease.state = "releasing"
                await join_cleanup(asyncio.create_task(self.revoke(vm_id, expected_lease=lease)))
                raise
            result = {"seq": seq, "action_seq": action_seq, "effect_state": "submitted"}
            lease.seq = seq
            lease.receipts[seq] = (fingerprint, result)
            while len(lease.receipts) > 128:
                lease.receipts.popitem(last=False)
            return result

    async def _watch(self) -> None:
        while True:
            due = time.perf_counter() + 0.25
            await asyncio.sleep(0.25)
            self.timings.record(loop_lag_ms=(time.perf_counter() - due) * 1000)
            for vm_id, lease in list(self.leases.items()):
                instance = self.service.manager.find(vm_id)
                if (
                    not lease.acquiring
                    and vm_id not in self._revocations
                    and lease.state == "held"
                    and (
                        lease.expires <= time.monotonic()
                        or instance is None
                        or instance.view.instance_id != lease.instance_id
                        or instance.view.boot_epoch != lease.boot_epoch
                        or instance.view.controller_epoch != lease.controller_epoch
                    )
                ):
                    self._revocations[vm_id] = asyncio.create_task(
                        self._expire(vm_id, lease), name="lease-expiry"
                    )

    async def _expire(self, vm_id: str, lease: Lease) -> None:
        try:
            async with self.service.manager.operations.hold(vm_id):
                if self.leases.get(vm_id) is not lease:
                    return
                try:
                    self.validate(vm_id, lease.token)
                except PaletteError:
                    await self.revoke(vm_id, expected_lease=lease)
        finally:
            self._revocations.pop(vm_id, None)
