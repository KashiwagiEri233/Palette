"""Local operator listener sharing the MCP service and its asyncio loop."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import secrets
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Self

from ..cleanup import close_all
from ..config import PaletteConfig
from ..errors import ErrorCode, PaletteError
from ..models import ErrorPayload, LifecycleResult
from ..service import PaletteService
from .listener import LocalListener
from .paths import IS_WINDOWS, RuntimeLease, RuntimePaths
from .protocol import (
    MAX_CLIENTS,
    OPERATION_TIMEOUT,
    Channel,
    Command,
    DesktopCommand,
    EmergencyStop,
    Endpoint,
    Request,
    Reset,
    Response,
    Start,
    Status,
    Stop,
    TakeControl,
    decode_json,
    receive_frame,
    send_frame,
)
from .requests import RequestRegistry

if TYPE_CHECKING:
    pass

logger = logging.getLogger("palette.operator")


def operation_budget(config: PaletteConfig) -> float:
    # Even a TCG boot and the bounded queue ahead of it fit the client/server deadline.
    budgets = [float(OPERATION_TIMEOUT)]
    for profile in config.profiles.values():
        limits = profile.timeouts
        budgets.append(
            limits.startup_seconds
            + limits.shutdown_seconds
            + limits.force_kill_seconds
            + profile.resources.max_action_seconds
            + limits.qmp_handshake_seconds
            + 3 * limits.qmp_command_seconds
            + 30.0
        )
    budget = max(budgets) * MAX_CLIENTS
    if not math.isfinite(budget):
        raise PaletteError(ErrorCode.INVALID_ARGUMENT, "operator timeouts must be finite")
    return budget


class OperatorServer:
    def __init__(self, service: PaletteService) -> None:
        self.service = service
        self.paths = RuntimePaths(service.config.runtime_root)
        self.lease = RuntimeLease(self.paths)
        generation = uuid.uuid4().hex
        self.endpoint = Endpoint(
            transport="pipe" if IS_WINDOWS else "unix",
            address=self.paths.address(generation),
            generation=generation,
            token=secrets.token_hex(32),
            operation_timeout_seconds=operation_budget(service.config),
        )
        self.listener = LocalListener(self.paths, self.endpoint, self._handle)
        self.requests = RequestRegistry[Response](pending_limit=MAX_CLIENTS, retained_limit=128)
        self.desktop_route: Callable[[DesktopCommand], Awaitable[dict[str, Any]]] | None = None
        # Leave six of the listener's eight slots available to short control requests.
        self._input_streams: set[tuple[str, str]] = set()

    def acquire(self) -> None:
        """Acquire before service startup; retain until service shutdown has completed."""
        self.lease.acquire()

    async def start(self) -> None:
        self.acquire()
        try:
            await self.listener.start()
        except BaseException:
            self.release()
            raise

    async def _handle(self, channel: Channel) -> None:
        binding: tuple[str, str] | None = None
        lease = None
        try:
            request = Request.model_validate(decode_json(await receive_frame(channel, timeout=2.0)))
            if not secrets.compare_digest(request.token, self.endpoint.token):
                return
            if request.generation != self.endpoint.generation:
                return
            command = request.command
            if isinstance(command, DesktopCommand) and command.method == "control.stream.open":
                token = command.payload.get("lease_id")
                if not command.vm_id or not isinstance(token, str):
                    raise PaletteError(ErrorCode.INVALID_ARGUMENT, "invalid input stream lease")
                lease = self.service.control.validate(command.vm_id, token)
                candidate = (command.vm_id, token)
                if len(self._input_streams) >= 2 or candidate in self._input_streams:
                    await send_frame(
                        channel,
                        self._failure(
                            request.request_id,
                            PaletteError(ErrorCode.BUSY, "input stream capacity reached"),
                        ),
                    )
                    lease = None
                    return
                binding = candidate
                self._input_streams.add(binding)
                await send_frame(
                    channel,
                    Response(
                        generation=self.endpoint.generation,
                        request_id=request.request_id,
                        ok=True,
                        result={"stream": "operator-input-stream-v1"},
                    ),
                )
                while True:
                    request = Request.model_validate(
                        decode_json(await self._receive_input_frame(channel, *binding))
                    )
                    command = request.command
                    if (
                        not secrets.compare_digest(request.token, self.endpoint.token)
                        or request.generation != self.endpoint.generation
                        or not isinstance(command, DesktopCommand)
                        or command.method != "control.input"
                        or command.vm_id != binding[0]
                        or command.payload.get("lease_id") != binding[1]
                    ):
                        return
                    self.service.control.validate(*binding)
                    response = await self._execute_once(request)
                    await send_frame(channel, response)
                    if not response.ok:
                        return
            response = await self._execute_once(request)
            await send_frame(channel, response)
        except (
            OSError,
            EOFError,
            TimeoutError,
            ValueError,
            PaletteError,
            asyncio.IncompleteReadError,
        ):
            # In particular, never log a failed authentication frame or validation payload.
            pass
        finally:
            try:
                await channel.close()
            finally:
                if binding is not None:
                    self._input_streams.discard(binding)
                    if lease is not None:
                        with contextlib.suppress(PaletteError):
                            await self.service.control.revoke(binding[0], expected_lease=lease)

    async def _receive_input_frame(self, channel: Channel, vm_id: str, token: str) -> bytes:
        # An idle authenticated stream may live for the lease's lifetime. Once a peer
        # starts a frame, the existing header+body deadline still protects against trickling.
        first = asyncio.create_task(channel.readexactly(1))
        try:
            while not first.done():
                self.service.control.validate(vm_id, token)
                await asyncio.wait({first}, timeout=0.25)
            prefix = first.result()
            self.service.control.validate(vm_id, token)
            return await receive_frame(channel, timeout=2.0, first_byte=prefix)
        finally:
            if not first.done():
                first.cancel()
            with contextlib.suppress(asyncio.CancelledError, OSError, EOFError):
                await first

    async def _execute_once(self, request: Request) -> Response:
        try:
            return await self.requests.execute(
                request.request_id,
                request.command.model_dump_json(),
                lambda: self._dispatch(request),
            )
        except PaletteError as exc:
            return self._failure(request.request_id, exc)

    def _failure(self, request_id: str, error: PaletteError) -> Response:
        return Response(
            generation=self.endpoint.generation,
            request_id=request_id,
            ok=False,
            error=ErrorPayload(**error.as_payload()),
        )

    async def _dispatch(self, request: Request) -> Response:
        try:
            async with asyncio.timeout(self.endpoint.operation_timeout_seconds):
                result = await self._route(request.command)
            return Response(
                generation=self.endpoint.generation,
                request_id=request.request_id,
                ok=True,
                result=result,
            )
        except PaletteError as exc:
            return self._failure(request.request_id, exc)
        except TimeoutError:
            return self._failure(
                request.request_id,
                PaletteError(
                    ErrorCode.ACTION_OUTCOME_UNKNOWN,
                    "operator operation exceeded its deadline; query status before retrying",
                ),
            )
        except Exception as exc:
            logger.error("operator operation failed: %s", type(exc).__name__)
            return self._failure(
                request.request_id,
                PaletteError(ErrorCode.INTERNAL_ERROR, "operator operation failed"),
            )

    async def _route(self, command: Command) -> dict[str, Any]:
        if isinstance(command, DesktopCommand):
            if self.desktop_route is None:
                raise PaletteError(
                    ErrorCode.UNSUPPORTED_CAPABILITY, "desktop owner is not available"
                )
            desktop_result: dict[str, Any] = await self.desktop_route(command)
            return desktop_result
        if isinstance(command, Start):
            result = await self.service.start(
                command.vm_id,
                actor="operator",
                profile_id=command.profile_id,
                image=command.image,
                image_format=command.image_format,
                iso=command.iso,
                extra_disks=command.extra_disks,
                qemu_args=command.qemu_args,
            )
            return self._lifecycle_payload(command.vm_id, result)
        if isinstance(command, Stop):
            result = await self.service.stop(command.vm_id, force=command.force, actor="operator")
            return self._lifecycle_payload(command.vm_id, result)
        if isinstance(command, Reset):
            result = await self.service.reset(
                command.vm_id, profile_id=command.profile_id, actor="operator"
            )
            return self._lifecycle_payload(command.vm_id, result)
        if isinstance(command, Status):
            return (await self.service.status(command.vm_id)).model_dump(mode="json")
        if isinstance(command, EmergencyStop):
            return {"stopped": await self.service.emergency_stop(command.vm_id)}
        if isinstance(command, TakeControl):
            epoch = await self.service.take_control(command.vm_id, reason=command.reason)
            return {"vm_id": command.vm_id, "controller_epoch": epoch}
        raise PaletteError(ErrorCode.INVALID_ARGUMENT, "unknown operator command")

    def _lifecycle_payload(self, vm_id: str, result: LifecycleResult) -> dict[str, Any]:
        # The legacy operator protocol omits unset optional fields for definitions.
        return result.model_dump(
            mode="json", exclude_unset=self.service.lifecycle.is_persistent(vm_id)
        )

    async def stop(self) -> None:
        await close_all((self.listener.close, self.requests.close))

    def release(self) -> None:
        self.lease.release()

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        try:
            await self.stop()
        finally:
            self.release()
