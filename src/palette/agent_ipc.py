"""A closed, separately authenticated Agent transport with binary image frames."""

from __future__ import annotations

import asyncio
import secrets
import struct
import uuid
from collections import OrderedDict
from typing import Any, Literal

from pydantic import Field

from .config import PaletteConfig
from .errors import ErrorCode, PaletteError
from .models import (
    ErrorPayload,
    GuestToolResult,
    LifecycleResult,
    ObserveResult,
    Receipt,
    ReleaseResult,
    StatusResult,
    WaitResult,
)
from .operator.client import OperatorClient
from .operator.paths import IS_WINDOWS, RuntimePaths
from .operator.protocol import (
    Channel,
    Endpoint,
    Response,
    StrictModel,
    decode_json,
    receive_frame,
    send_frame,
)
from .operator.server import OperatorServer
from .operator.unix import UnixChannel
from .service import PaletteService

MAX_IMAGE_BYTES = 8 * 1024 * 1024


class AgentCommand(StrictModel):
    method: Literal[
        "status",
        "observe_image",
        "act",
        "wait",
        "release_inputs",
        "guest_input_text",
        "guest_launch_app",
        "start",
        "stop",
        "reset",
    ]
    parameters: dict[str, Any]


class AgentRequest(StrictModel):
    version: Literal[1] = 1
    generation: str
    token: str
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    command: AgentCommand


class AgentResponse(Response):
    image_bytes: int = Field(default=0, ge=0, le=MAX_IMAGE_BYTES)


class AgentServer(OperatorServer):
    def __init__(
        self, service: PaletteService, operator: OperatorServer, desktop: Any = None
    ) -> None:
        super().__init__(service)
        self.desktop = desktop
        self.paths = RuntimePaths(service.config.runtime_root / ".agent-ipc")
        self.endpoint = self.endpoint.model_copy(
            update={"address": self.paths.address(self.endpoint.generation)}
        )
        self._agent_requests: OrderedDict[
            str, tuple[str, asyncio.Task[tuple[AgentResponse, bytes]]]
        ] = OrderedDict()

    def acquire(self) -> None:
        self.paths.prepare()

    def release(self) -> None:
        pass

    async def _handle(self, channel: Channel) -> None:
        try:
            request = AgentRequest.model_validate(
                decode_json(await receive_frame(channel, timeout=2))
            )
            if request.generation != self.endpoint.generation or not secrets.compare_digest(
                request.token, self.endpoint.token
            ):
                return
            fingerprint = request.command.model_dump_json()
            cached = self._agent_requests.get(request.request_id)
            if cached is not None and cached[0] != fingerprint:
                raise PaletteError(ErrorCode.REQUEST_ID_CONFLICT, "Agent request arguments changed")
            if cached is None:
                if sum(not task.done() for _, task in self._agent_requests.values()) >= 8:
                    raise PaletteError(ErrorCode.BUSY, "Agent request capacity reached")
                task = asyncio.create_task(self._execute_agent(request))
                self._agent_requests[request.request_id] = (fingerprint, task)
                task.add_done_callback(lambda _: self._trim_agent_requests())
            else:
                task = cached[1]
            response, image = await asyncio.shield(task)
            await send_frame(channel, response)
            await channel.write(struct.pack(">I", len(image)) + image)
            for key, (_, completed) in list(self._agent_requests.items()):
                if len(self._agent_requests) <= 4:
                    break
                if completed.done():
                    self._agent_requests.pop(key, None)
        except (
            OSError,
            EOFError,
            ValueError,
            PaletteError,
            TimeoutError,
            asyncio.IncompleteReadError,
        ):
            pass
        finally:
            await channel.close()

    def _trim_agent_requests(self) -> None:
        for key, (_, task) in list(self._agent_requests.items()):
            if len(self._agent_requests) <= 4:
                break
            if task.done():
                self._agent_requests.pop(key, None)

    async def _execute_agent(self, request: AgentRequest) -> tuple[AgentResponse, bytes]:
        image = b""
        try:
            parameters = dict(request.command.parameters)
            if "actor" in parameters or "vm_id" not in parameters:
                raise PaletteError(ErrorCode.INVALID_ARGUMENT, "invalid Agent parameters")
            vm_id = parameters.pop("vm_id")
            method = request.command.method
            result: Any
            async with asyncio.timeout(self.endpoint.operation_timeout_seconds):
                if (
                    self.desktop is not None
                    and vm_id in self.desktop.definitions
                    and method in {"start", "stop", "reset"}
                ):
                    self.service.control.require_agent(vm_id)
                    definition = self.desktop.get(vm_id)
                    from .policy import PathGuard, Policy

                    policy = Policy(definition.profile, PathGuard(self.service.config.runtime_root))
                    policy.require_lifecycle(
                        "force_stop" if method == "stop" and parameters.get("force") else method,
                        actor="agent",
                    )
                    if method == "start":
                        if parameters.get("profile_id") not in (None, definition.draft.profile_id):
                            raise PaletteError(
                                ErrorCode.INVALID_ARGUMENT,
                                "persistent VM profile is configured by the operator",
                            )
                        if any(
                            parameters.get(key) is not None
                            for key in ("image", "image_format", "iso", "extra_disks", "qemu_args")
                        ):
                            raise PaletteError(
                                ErrorCode.INVALID_ARGUMENT,
                                "persistent VM storage is configured by the operator",
                            )
                        info = await self.desktop.start(vm_id)
                    elif method == "stop":
                        info = await self.desktop.stop(
                            vm_id,
                            force=parameters.get("force", False),
                            expected_instance_id=parameters.get("expected_instance_id"),
                        )
                    else:
                        info = await self.desktop.reset(vm_id)
                    result = LifecycleResult(ok=True, **info)
                elif method == "status":
                    result = await self.service.status(vm_id, **parameters)
                elif method == "observe_image":
                    result, image = await self.service.observe_image(vm_id, **parameters)
                elif method == "act":
                    result, png = await self.service.act(vm_id, **parameters)
                    image = png or b""
                elif method == "wait":
                    result, png = await self.service.wait(vm_id, **parameters)
                    image = png or b""
                elif method == "release_inputs":
                    result = await self.service.release_inputs(vm_id, **parameters)
                elif method == "guest_input_text":
                    result, png = await self.service.guest_input_text(vm_id, **parameters)
                    image = png or b""
                elif method == "guest_launch_app":
                    result, png = await self.service.guest_launch_app(vm_id, **parameters)
                    image = png or b""
                elif method == "start":
                    result = await self.service.start(vm_id, actor="agent", **parameters)
                elif method == "stop":
                    result = await self.service.stop(vm_id, actor="agent", **parameters)
                else:
                    result = await self.service.reset(vm_id, actor="agent", **parameters)
            if len(image) > MAX_IMAGE_BYTES:
                raise PaletteError(
                    ErrorCode.CAPTURE_TOO_LARGE, "image exceeds Agent transport limit"
                )
            return AgentResponse(
                generation=self.endpoint.generation,
                request_id=request.request_id,
                ok=True,
                result=result.model_dump(mode="json"),
                image_bytes=len(image),
            ), image
        except (PaletteError, TypeError, ValueError) as exc:
            error = (
                exc
                if isinstance(exc, PaletteError)
                else PaletteError(ErrorCode.INVALID_ARGUMENT, "invalid Agent request")
            )
            return AgentResponse(
                generation=self.endpoint.generation,
                request_id=request.request_id,
                ok=False,
                error=ErrorPayload(**error.as_payload()),
            ), b""
        except TimeoutError:
            return AgentResponse(
                generation=self.endpoint.generation,
                request_id=request.request_id,
                ok=False,
                error=ErrorPayload(
                    **PaletteError(
                        ErrorCode.ACTION_OUTCOME_UNKNOWN, "Agent operation outcome is unknown"
                    ).as_payload()
                ),
            ), b""

    async def stop(self) -> None:
        for _, task in self._agent_requests.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for _, task in self._agent_requests.values()), return_exceptions=True
        )
        self._agent_requests.clear()
        await super().stop()


class RemoteService(PaletteService):
    """MCP adapter; never owns or shuts down the VM service."""

    def __init__(
        self, config: PaletteConfig, endpoint: Endpoint, *, default_profile: str | None = None
    ) -> None:
        self.config = config
        self.endpoint = endpoint
        self.default_profile = default_profile

    async def _call(
        self, method: str, vm_id: str, parameters: dict[str, Any]
    ) -> tuple[dict[str, Any], bytes | None]:
        endpoint = self.endpoint
        if IS_WINDOWS:
            from .operator.windows import connect

            channel: Channel = await connect(endpoint.address)
        else:
            open_unix: Any = getattr(asyncio, "open_unix_connection", None)
            reader, writer = await open_unix(endpoint.address)
            channel = UnixChannel(reader, writer)
        command = AgentCommand.model_validate(
            {"method": method, "parameters": {"vm_id": vm_id, **parameters}}
        )
        request = AgentRequest(
            generation=endpoint.generation,
            token=endpoint.token,
            request_id=uuid.uuid4().hex,
            command=command,
        )
        try:
            await send_frame(channel, request)
            response = AgentResponse.model_validate(
                decode_json(
                    await receive_frame(channel, timeout=endpoint.operation_timeout_seconds)
                )
            )
            if (
                response.request_id != request.request_id
                or response.generation != endpoint.generation
            ):
                raise ValueError("Agent response identity mismatch")
            size = struct.unpack(">I", await channel.readexactly(4))[0]
            if size != response.image_bytes or size > MAX_IMAGE_BYTES:
                raise ValueError("Agent image length mismatch")
            async with asyncio.timeout(10):
                image = await channel.readexactly(size)
            if response.error is not None:
                raise PaletteError(
                    ErrorCode(response.error.code),
                    response.error.message,
                    details=response.error.details,
                    retryable=response.error.retryable,
                )
            return response.result or {}, image or None
        except (OSError, EOFError, ValueError, TimeoutError, asyncio.IncompleteReadError) as exc:
            raise PaletteError(
                ErrorCode.ACTION_OUTCOME_UNKNOWN,
                "Agent connection ended before its result was confirmed",
                retryable=False,
            ) from exc
        finally:
            await channel.close()

    async def status(self, vm_id: str, *, actor: Any = "agent") -> StatusResult:
        data, _ = await self._call("status", vm_id, {})
        return StatusResult.model_validate(data)

    async def observe_image(
        self, vm_id: str, *, request_id: str | None = None, max_long_edge: int | None = None
    ) -> tuple[ObserveResult, bytes]:
        data, image = await self._call(
            "observe_image", vm_id, {"request_id": request_id, "max_long_edge": max_long_edge}
        )
        return ObserveResult.model_validate(data), image or b""

    async def act(
        self,
        vm_id: str,
        *,
        instance_id: str,
        frame_id: str,
        request_id: str,
        action: dict[str, Any],
    ) -> tuple[Receipt, bytes | None]:
        data, image = await self._call(
            "act",
            vm_id,
            {
                "instance_id": instance_id,
                "frame_id": frame_id,
                "request_id": request_id,
                "action": action,
            },
        )
        return Receipt.model_validate(data), image

    async def wait(
        self,
        vm_id: str,
        *,
        after_frame_id: str | None,
        mode: str,
        timeout_ms: int,
        delay_ms: int | None = None,
        threshold: float = 0.005,
    ) -> tuple[WaitResult, bytes | None]:
        data, image = await self._call(
            "wait",
            vm_id,
            {
                "after_frame_id": after_frame_id,
                "mode": mode,
                "timeout_ms": timeout_ms,
                "delay_ms": delay_ms,
                "threshold": threshold,
            },
        )
        return WaitResult.model_validate(data), image

    async def release_inputs(self, vm_id: str, *, instance_id: str) -> ReleaseResult:
        data, _ = await self._call("release_inputs", vm_id, {"instance_id": instance_id})
        return ReleaseResult.model_validate(data)

    async def start(
        self,
        vm_id: str,
        *,
        profile_id: str | None = None,
        actor: Any = "agent",
        image: str | None = None,
        image_format: str | None = None,
        iso: Any = None,
        extra_disks: Any = None,
        qemu_args: Any = None,
    ) -> LifecycleResult:
        data, _ = await self._call(
            "start",
            vm_id,
            {
                "profile_id": profile_id or self.default_profile,
                "image": image,
                "image_format": image_format,
                "iso": iso,
                "extra_disks": extra_disks,
                "qemu_args": qemu_args,
            },
        )
        return LifecycleResult.model_validate(data)

    async def stop(
        self,
        vm_id: str,
        *,
        force: bool = False,
        actor: Any = "agent",
        expected_instance_id: str | None = None,
    ) -> LifecycleResult:
        parameters: dict[str, Any] = {"force": force}
        if expected_instance_id is not None:
            parameters["expected_instance_id"] = expected_instance_id
        data, _ = await self._call("stop", vm_id, parameters)
        return LifecycleResult.model_validate(data)

    async def reset(
        self, vm_id: str, *, profile_id: str | None = None, actor: Any = "agent"
    ) -> LifecycleResult:
        data, _ = await self._call("reset", vm_id, {"profile_id": profile_id})
        return LifecycleResult.model_validate(data)

    async def guest_input_text(
        self,
        vm_id: str,
        *,
        instance_id: str,
        boot_epoch: int,
        frame_id: str | None,
        request_id: str,
        text: str,
        method: str,
        target: str = "focused",
    ) -> tuple[GuestToolResult, bytes | None]:
        data, image = await self._call(
            "guest_input_text",
            vm_id,
            {
                "instance_id": instance_id,
                "boot_epoch": boot_epoch,
                "frame_id": frame_id,
                "request_id": request_id,
                "text": text,
                "method": method,
                "target": target,
            },
        )
        return GuestToolResult.model_validate(data), image

    async def guest_launch_app(
        self,
        vm_id: str,
        *,
        instance_id: str,
        boot_epoch: int,
        request_id: str,
        app_id: str,
        options: dict[str, Any],
    ) -> tuple[GuestToolResult, bytes | None]:
        data, image = await self._call(
            "guest_launch_app",
            vm_id,
            {
                "instance_id": instance_id,
                "boot_epoch": boot_epoch,
                "request_id": request_id,
                "app_id": app_id,
                "options": options,
            },
        )
        return GuestToolResult.model_validate(data), image


async def discover(config: PaletteConfig) -> dict[str, Any] | None:
    client = OperatorClient(config.runtime_root)
    try:
        info = await client.call("hello")
    except PaletteError as exc:
        if exc.code == ErrorCode.PRECONDITION_FAILED:
            return None
        raise
    if info.get("desktop_protocol") != 1:
        raise PaletteError(ErrorCode.PRECONDITION_FAILED, "incompatible runtime owner")
    return info
