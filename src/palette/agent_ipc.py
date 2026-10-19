"""A closed, separately authenticated Agent transport with binary image frames."""

from __future__ import annotations

import asyncio
import secrets
import struct
import uuid
from typing import Any, Literal

from pydantic import Field

from .cleanup import close_all
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
    VmListResult,
    WaitResult,
)
from .operator.client import OperatorClient
from .operator.listener import LocalListener
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
from .operator.requests import RequestRegistry
from .operator.server import OperatorServer
from .operator.unix import UnixChannel
from .service import PaletteService

MAX_IMAGE_BYTES = 8 * 1024 * 1024


class AgentCommand(StrictModel):
    method: Literal[
        "list_vms",
        "status",
        "observe_image",
        "act",
        "wait",
        "release_inputs",
        "guest_input_text",
        "guest_launch_app",
        "guest_enhancement",
        "start",
        "stop",
        "reset",
        "driver_acquire",
        "driver_heartbeat",
        "driver_release",
    ]
    parameters: dict[str, Any]


class AgentRequest(StrictModel):
    version: Literal[1] = 1
    generation: str
    token: str
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    command: AgentCommand
    claim_token: str | None = Field(default=None, pattern=r"^[a-f0-9]{48}$")


class AgentResponse(Response):
    image_bytes: int = Field(default=0, ge=0, le=MAX_IMAGE_BYTES)


class AgentServer:
    def __init__(self, service: PaletteService, operator: OperatorServer) -> None:
        self.service = service
        self.paths = RuntimePaths(service.config.runtime_root / ".agent-ipc")
        generation = uuid.uuid4().hex
        self.endpoint = operator.endpoint.model_copy(
            update={
                "address": self.paths.address(generation),
                "generation": generation,
                "token": secrets.token_hex(32),
            }
        )
        self.listener = LocalListener(self.paths, self.endpoint, self._handle)
        self.requests = RequestRegistry[tuple[AgentResponse, bytes]](
            pending_limit=8, retained_limit=4
        )

    async def start(self) -> None:
        await self.listener.start()

    async def _handle(self, channel: Channel) -> None:
        try:
            request = AgentRequest.model_validate(
                decode_json(await receive_frame(channel, timeout=2))
            )
            if request.generation != self.endpoint.generation or not secrets.compare_digest(
                request.token, self.endpoint.token
            ):
                return
            try:
                response, image = await self.requests.execute(
                    request.request_id,
                    request.command.model_dump_json() + (request.claim_token or ""),
                    lambda: self._execute_agent(request),
                )
            except PaletteError as exc:
                response = AgentResponse(
                    generation=self.endpoint.generation,
                    request_id=request.request_id,
                    ok=False,
                    error=ErrorPayload(**exc.as_payload()),
                )
                image = b""
            async with asyncio.timeout(10):
                await send_frame(channel, response)
                await channel.write(struct.pack(">I", len(image)) + image)
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

    async def _execute_agent(self, request: AgentRequest) -> tuple[AgentResponse, bytes]:
        with self.service.driver.bind(request.claim_token):
            return await self._execute_bound(request)

    async def _execute_bound(self, request: AgentRequest) -> tuple[AgentResponse, bytes]:
        image = b""
        try:
            parameters = dict(request.command.parameters)
            method = request.command.method
            if (
                "actor" in parameters
                or (method == "list_vms" and parameters)
                or (method != "list_vms" and "vm_id" not in parameters)
            ):
                raise PaletteError(ErrorCode.INVALID_ARGUMENT, "invalid Agent parameters")
            vm_id = parameters.pop("vm_id", "")
            result: Any
            async with asyncio.timeout(self.endpoint.operation_timeout_seconds):
                if method == "list_vms":
                    result = await self.service.list_vms()
                elif method == "driver_acquire":
                    result = await self.service.driver.acquire(vm_id, **parameters)
                elif method == "driver_heartbeat":
                    if parameters:
                        raise ValueError("unexpected driver parameters")
                    result = self.service.driver.heartbeat(vm_id)
                elif method == "driver_release":
                    if parameters:
                        raise ValueError("unexpected driver parameters")
                    result = await self.service.driver.release(vm_id)
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
                elif method == "guest_enhancement":
                    result, png = await self.service.guest_enhancement(vm_id, **parameters)
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
                result=result if isinstance(result, dict) else result.model_dump(mode="json"),
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
        except Exception:
            return AgentResponse(
                generation=self.endpoint.generation,
                request_id=request.request_id,
                ok=False,
                error=ErrorPayload(
                    **PaletteError(ErrorCode.INTERNAL_ERROR, "Agent operation failed").as_payload()
                ),
            ), b""

    async def stop(self) -> None:
        await close_all((self.listener.close, self.requests.close))


class RemoteService:
    """MCP adapter; never owns or shuts down the VM service."""

    def __init__(
        self, config: PaletteConfig, endpoint: Endpoint, *, default_profile: str | None = None
    ) -> None:
        self.config = config
        self.endpoint = endpoint
        self.default_profile = default_profile

    async def _call(
        self, method: str, vm_id: str | None, parameters: dict[str, Any]
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
            {
                "method": method,
                "parameters": {**({"vm_id": vm_id} if vm_id is not None else {}), **parameters},
            }
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
            async with asyncio.timeout(10):
                size = struct.unpack(">I", await channel.readexactly(4))[0]
                if size != response.image_bytes or size > MAX_IMAGE_BYTES:
                    raise ValueError("Agent image length mismatch")
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

    async def list_vms(self) -> VmListResult:
        data, _ = await self._call("list_vms", None, {})
        return VmListResult.model_validate(data)

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

    async def guest_enhancement(
        self,
        vm_id: str,
        *,
        instance_id: str,
        boot_epoch: int,
        request_id: str,
        operation: str,
        parameters: dict[str, Any],
    ) -> tuple[GuestToolResult, bytes | None]:
        data, image = await self._call(
            "guest_enhancement",
            vm_id,
            {
                "instance_id": instance_id,
                "boot_epoch": boot_epoch,
                "request_id": request_id,
                "operation": operation,
                "parameters": parameters,
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
