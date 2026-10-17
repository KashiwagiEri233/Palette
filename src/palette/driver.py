"""Run-scoped Agent ownership, independent of the authenticated transport."""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .errors import ErrorCode, PaletteError
from .policy import validate_identifier

if TYPE_CHECKING:
    from .service import PaletteService

_token: ContextVar[str | None] = ContextVar("agent_driver_token", default=None)


@dataclass
class Driver:
    token: str
    run_id: str
    instance_id: str
    boot_epoch: int
    expires: float
    faulted: bool = False


class DriverManager:
    def __init__(self, service: PaletteService) -> None:
        self.service = service
        self.claims: dict[str, Driver] = {}
        self._watcher: asyncio.Task[None] | None = None

    @contextmanager
    def bind(self, token: str | None) -> Iterator[None]:
        marker = _token.set(token)
        try:
            yield
        finally:
            _token.reset(marker)

    def require(self, vm_id: str) -> None:
        claim = self.claims.get(vm_id)
        token = _token.get()
        if claim is None:
            if token is not None:
                raise PaletteError(ErrorCode.STALE_FRAME, "the Agent run no longer owns this VM")
            return
        instance = self.service.manager.find(vm_id)
        if (
            token is None
            or not secrets.compare_digest(claim.token, token)
            or claim.faulted
            or claim.expires <= time.monotonic()
            or instance is None
            or instance.view.instance_id != claim.instance_id
            or instance.view.boot_epoch != claim.boot_epoch
        ):
            raise PaletteError(ErrorCode.BUSY, "another Agent run owns this VM")

    async def acquire(self, vm_id: str, *, instance_id: str, run_id: str) -> dict[str, Any]:
        validate_identifier(run_id, what="run_id")
        async with self.service.manager.operations.hold(vm_id):
            self.service.manager.operations.require_accepting()
            instance = await self.service.manager.require_running(vm_id)
            self.service.require_instance_id(instance, instance_id)
            self.service.control.require_agent(vm_id)
            if vm_id in self.claims:
                raise PaletteError(ErrorCode.BUSY, "another Agent run owns this VM")
            claim = Driver(
                secrets.token_hex(24),
                run_id,
                instance_id,
                instance.view.boot_epoch,
                time.monotonic() + 3,
            )
            self.claims[vm_id] = claim
            try:
                epoch = await self.service.manager.take_control(
                    vm_id, expected_instance_id=instance_id
                )
                self.service.record_event(
                    "control.handover", vm_id, epoch=epoch, reason="agent_run"
                )
                if any(self.service.controller_for(instance).ledger_snapshot().values()):
                    claim.faulted = True
                    raise PaletteError(ErrorCode.QMP_UNAVAILABLE, "input release was not confirmed")
                claim.expires = time.monotonic() + 3
            except BaseException:
                if not claim.faulted:
                    self.claims.pop(vm_id, None)
                raise
            return {"claim_token": claim.token, "expires_after_ms": 3000}

    def heartbeat(self, vm_id: str) -> dict[str, Any]:
        self.require(vm_id)
        claim = self.claims.get(vm_id)
        if claim is None:
            raise PaletteError(ErrorCode.STALE_FRAME, "the Agent claim is missing")
        claim.expires = time.monotonic() + 3
        return {"expires_after_ms": 3000}

    async def release(self, vm_id: str) -> dict[str, Any]:
        claim = self.claims.get(vm_id)
        if claim is None:
            return {"release_confirmed": True}
        token = _token.get()
        if token is None or not secrets.compare_digest(token, claim.token):
            raise PaletteError(ErrorCode.POLICY_DENIED, "this claim belongs to another run")
        return {"release_confirmed": await self.revoke(vm_id, expected=claim)}

    async def revoke(self, vm_id: str, *, expected: Driver | None = None) -> bool:
        async with self.service.manager.operations.hold(vm_id):
            claim = self.claims.get(vm_id)
            if claim is None or (expected is not None and claim is not expected):
                return True
            claim.faulted = True
            instance = self.service.manager.find(vm_id)
            if instance is None or instance.view.instance_id != claim.instance_id:
                self.claims.pop(vm_id, None)
                return True
            # Advance the epoch before cancellation so no old frame can be reused.
            instance.view.controller_epoch += 1
            controller = instance.controller
            confirmed = (
                controller is None or (await controller.emergency_release()).release_confirmed
            )
            if confirmed or instance.process is None or instance.process.returncode is not None:
                self.claims.pop(vm_id, None)
            return confirmed

    def start(self) -> None:
        if self._watcher is None or self._watcher.done():
            self._watcher = asyncio.create_task(self._watch(), name="agent-driver-claims")

    async def close(self) -> None:
        if self._watcher is not None:
            self._watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watcher
        for vm_id in list(self.claims):
            await self.revoke(vm_id)

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(0.25)
            for vm_id, claim in list(self.claims.items()):
                instance = self.service.manager.find(vm_id)
                if not claim.faulted and (
                    claim.expires <= time.monotonic()
                    or instance is None
                    or instance.view.instance_id != claim.instance_id
                    or instance.view.boot_epoch != claim.boot_epoch
                ):
                    await self.revoke(vm_id, expected=claim)
