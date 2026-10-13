"""Local operator listener sharing the MCP service and its asyncio loop."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import secrets
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self

from ..config import PaletteConfig
from ..errors import ErrorCode, PaletteError
from ..models import ErrorPayload
from ..service import PaletteService
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
from .unix import UnixChannel

if TYPE_CHECKING:
    from .windows import WindowsListener

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
        self._unix: asyncio.AbstractServer | None = None
        self._pipe: WindowsListener | None = None
        self._accept_task: asyncio.Task[None] | None = None
        self._handlers: set[asyncio.Task[None]] = set()
        self._requests: OrderedDict[str, tuple[str, asyncio.Task[Response]]] = OrderedDict()
        self._closing = False
        self._socket_identity: tuple[int, int] | None = None
        self.desktop_route: Any = None
        self.desktop_manager: Any = None

    def acquire(self) -> None:
        """Acquire before service startup; retain until service shutdown has completed."""
        self.lease.acquire()

    async def start(self) -> None:
        self.acquire()
        try:
            if IS_WINDOWS:
                from .windows import WindowsListener

                self._pipe = WindowsListener(self.endpoint.address)
                self._accept_task = asyncio.create_task(
                    self._accept_pipes(), name="operator-accept"
                )
            else:
                self.paths.remove_stale_socket(self.endpoint.generation)
                start_unix: Any = getattr(asyncio, "start_unix_server", None)
                self._unix = await start_unix(
                    self._accept_unix,
                    path=self.endpoint.address,
                    limit=256 * 1024,
                    backlog=MAX_CLIENTS,
                )
                path = Path(self.endpoint.address)
                os.chmod(path, 0o600)
                info = path.stat()
                self._socket_identity = (info.st_dev, info.st_ino)
            self.paths.publish(self.endpoint)
        except BaseException:
            await self.stop()
            self.release()
            raise

    def _accept_unix(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._schedule(UnixChannel(reader, writer))

    async def _accept_pipes(self) -> None:
        if self._pipe is None:
            return
        while True:
            if self._closing:
                return
            try:
                channel = await self._pipe.accept()
            except OSError:
                logger.info("operator pipe accept stopped")
                return
            self._schedule(channel)

    def _schedule(self, channel: Channel) -> None:
        if self._closing or len(self._handlers) >= MAX_CLIENTS:
            task = asyncio.create_task(channel.close(), name="operator-reject")
        else:
            task = asyncio.create_task(self._handle(channel), name="operator-client")
        self._handlers.add(task)
        task.add_done_callback(self._handlers.discard)

    async def _handle(self, channel: Channel) -> None:
        try:
            request = Request.model_validate(decode_json(await receive_frame(channel, timeout=2.0)))
            if not secrets.compare_digest(request.token, self.endpoint.token):
                return
            if request.generation != self.endpoint.generation:
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
            await channel.close()

    async def _execute_once(self, request: Request) -> Response:
        fingerprint = request.command.model_dump_json()
        previous = self._requests.get(request.request_id)
        if previous is not None:
            if previous[0] != fingerprint:
                return self._failure(
                    request.request_id,
                    PaletteError(
                        ErrorCode.REQUEST_ID_CONFLICT, "operator request_id has different arguments"
                    ),
                )
            return await asyncio.shield(previous[1])
        if sum(not task.done() for _, task in self._requests.values()) >= MAX_CLIENTS:
            return self._failure(
                request.request_id,
                PaletteError(ErrorCode.BUSY, "the operator request limit has been reached"),
            )
        task = asyncio.create_task(
            self._dispatch(request), name=f"operator-{request.command.method}"
        )
        self._requests[request.request_id] = (fingerprint, task)
        task.add_done_callback(lambda _: self._trim_requests())
        return await asyncio.shield(task)

    def _trim_requests(self) -> None:
        for key, (_, task) in list(self._requests.items()):
            if len(self._requests) <= 128:
                break
            if task.done():
                self._requests.pop(key, None)

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
            if (
                self.desktop_manager is not None
                and command.vm_id in self.desktop_manager.definitions
            ):
                if (
                    command.profile_id is not None
                    and command.profile_id
                    != self.desktop_manager.get(command.vm_id).draft.profile_id
                ):
                    raise PaletteError(
                        ErrorCode.INVALID_ARGUMENT,
                        "persistent VM profile is configured by the desktop definition",
                    )
                if any(
                    value is not None
                    for value in (
                        command.image,
                        command.image_format,
                        command.iso,
                        command.extra_disks,
                        command.qemu_args,
                    )
                ):
                    raise PaletteError(
                        ErrorCode.INVALID_ARGUMENT,
                        "persistent storage is configured by the desktop definition",
                    )
                started: dict[str, Any] = await self.desktop_manager.start(command.vm_id)
                return {"ok": True, **started}
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
            return result.model_dump(mode="json")
        if isinstance(command, Stop):
            if (
                self.desktop_manager is not None
                and command.vm_id in self.desktop_manager.definitions
            ):
                stopped: dict[str, Any] = await self.desktop_manager.stop(
                    command.vm_id, force=command.force
                )
                return {"ok": True, **stopped}
            return (
                await self.service.stop(command.vm_id, force=command.force, actor="operator")
            ).model_dump(mode="json")
        if isinstance(command, Reset):
            if (
                self.desktop_manager is not None
                and command.vm_id in self.desktop_manager.definitions
            ):
                reset: dict[str, Any] = await self.desktop_manager.reset(command.vm_id)
                return {"ok": True, **reset}
            return (
                await self.service.reset(
                    command.vm_id, profile_id=command.profile_id, actor="operator"
                )
            ).model_dump(mode="json")
        if isinstance(command, Status):
            return (await self.service.status(command.vm_id)).model_dump(mode="json")
        if isinstance(command, EmergencyStop):
            return {"stopped": await self.service.emergency_stop(command.vm_id)}
        if isinstance(command, TakeControl):
            epoch = await self.service.take_control(command.vm_id, reason=command.reason)
            return {"vm_id": command.vm_id, "controller_epoch": epoch}
        raise PaletteError(ErrorCode.INVALID_ARGUMENT, "unknown operator command")

    async def stop(self) -> None:
        self._closing = True
        self.paths.retire(self.endpoint.generation)
        if self._unix is not None:
            self._unix.close()
            await self._unix.wait_closed()
            self._unix = None
        if self._pipe is not None:
            await self._pipe.close()
            self._pipe = None
        if self._accept_task is not None:
            self._accept_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._accept_task
            self._accept_task = None
        tasks = set(self._handlers) | {
            task for _, task in self._requests.values() if not task.done()
        }
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._requests.clear()
        self._remove_socket()

    def _remove_socket(self) -> None:
        if self._socket_identity is None:
            return
        path = Path(self.endpoint.address)
        with contextlib.suppress(FileNotFoundError):
            info = path.lstat()
            if (info.st_dev, info.st_ino) == self._socket_identity:
                path.unlink()
        self._socket_identity = None

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
