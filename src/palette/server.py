"""The MCP tool surface.

Five tools carry the visual baseline (``vm_status``, ``vm_observe``, ``vm_act``,
``vm_wait``, ``vm_release_inputs``); three drive the lifecycle and are refused unless the
trusted profile grants an agent that authority; two more expose the retained guest
enhancement channel and are only registered when a profile enables it.

Every success and every structured failure share one published output schema per tool, so
a client does not need a second, undocumented error shape. Images are returned as
``ImageContent`` with the metadata both as ``structuredContent`` and as serialized
``TextContent``, because not every client forwards image blocks to a vision model and the
plan requires verifying the real target client rather than assuming.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from anyio import CancelScope
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations

from .errors import ErrorCode, PaletteError
from .models import (
    Action,
    GuestToolResult,
    LifecycleResult,
    ObserveResult,
    Receipt,
    ReleaseResult,
    StatusResult,
    WaitResult,
)
from .service import PaletteService

logger = logging.getLogger("palette.server")

READ_ONLY = ToolAnnotations(read_only_hint=True)
CONTROL = ToolAnnotations(read_only_hint=False, idempotent_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True)

INSTRUCTIONS = """\
Palette gives you a QEMU virtual desktop you can see and operate.

Work in this loop: call vm_observe to get a screenshot, decide, call vm_act with the
coordinates you saw in that screenshot, then look at the screenshot the action returns.

Things that will save you time:

* Coordinates are pixels in the image you actually received. Always act from the newest
  frame; an action that names an old frame is rejected with STALE_FRAME and you should
  simply observe again.
* After every action the desktop may have changed: use the frame returned by vm_act
  rather than an older one.
* type_text sends key events for US-layout printable ASCII only. Control characters are
  refused: send an explicit ENTER or TAB key action instead. Anything else fails with
  UNSUPPORTED_TEXT and nothing is typed.
* An action that times out or loses the connection may already have taken effect. Do not
  repeat it blindly - observe first.
* A successful call means the input reached the virtual machine. It does not mean an
  application reacted. Verify with your eyes.
"""


def build_server(service: PaletteService) -> MCPServer:
    """Wire the service into an MCP server. All authorization stays server-side."""

    @asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[dict[str, Any]]:
        from .agent_ipc import RemoteService
        from .runtime import RuntimeOwner

        if isinstance(service, RemoteService):
            yield {}
            return
        owner = RuntimeOwner(service)
        try:
            await owner.start()
            yield {}
        finally:
            with CancelScope(shield=True):
                await owner.close()

    server: MCPServer = MCPServer(
        name="palette",
        title="Palette virtual desktop",
        instructions=INSTRUCTIONS,
        lifespan=lifespan,
    )

    register_baseline_tools(server, service)
    register_lifecycle_tools(server, service)
    if any(profile.guest_tools.enabled for profile in service.config.profiles.values()):
        register_guest_tools(server, service)
    return server


def register_baseline_tools(server: MCPServer, service: PaletteService) -> None:
    @server.tool(
        name="vm_status",
        title="VM status",
        annotations=READ_ONLY,
        description=(
            "Report the lifecycle state and the separate readiness levels of a VM, "
            "together with its capabilities and what the service currently believes is "
            "held down on the keyboard and pointer."
        ),
    )
    async def vm_status(vm_id: str) -> Annotated[CallToolResult, StatusResult]:
        try:
            result = await service.status(vm_id)
        except PaletteError as exc:
            return _failure(StatusResult, exc)
        # A status request for an unknown VM is a business failure: report it as one rather
        # than as a successful call that happens to say ok=false.
        return _success(result, is_error=not result.ok)

    @server.tool(
        name="vm_observe",
        title="Look at the desktop",
        annotations=READ_ONLY,
        description=(
            "Capture the VM's screen. Returns the image plus a frame describing the "
            "exact coordinate space it defines. Use the numbers from this frame in "
            "vm_act; do not reuse a frame after anything else has happened."
        ),
    )
    async def vm_observe(
        vm_id: str,
        max_long_edge: int | None = None,
    ) -> Annotated[CallToolResult, ObserveResult]:
        try:
            result, image = await service.observe_image(vm_id, max_long_edge=max_long_edge)
        except PaletteError as exc:
            return _failure(ObserveResult, exc)
        return _success(result, image=image)

    @server.tool(
        name="vm_act",
        title="Operate the desktop",
        annotations=CONTROL,
        description=(
            "Perform one bounded desktop action: move, click, double-click, vertical "
            "scroll, drag, a keyboard chord, or US-ASCII text. Requires the frame_id you "
            "derived the coordinates from. Returns a receipt and, on success, a fresh "
            "screenshot."
        ),
    )
    async def vm_act(
        vm_id: str,
        instance_id: str,
        frame_id: str,
        request_id: str,
        action: Action,
    ) -> Annotated[CallToolResult, Receipt]:
        payload = action.model_dump(mode="json") if hasattr(action, "model_dump") else dict(action)
        try:
            receipt, image = await service.act(
                vm_id,
                instance_id=instance_id,
                frame_id=frame_id,
                request_id=request_id,
                action=payload,
            )
        except PaletteError as exc:
            return _failure(Receipt, exc, request_id=request_id, effect_state="not_sent")
        return _success(receipt, image=image, is_error=not receipt.ok)

    @server.tool(
        name="vm_wait",
        title="Wait for the screen",
        annotations=READ_ONLY,
        description=(
            "Wait up to 10 seconds before looking again: 'delay' simply waits, 'changed' "
            "waits for pixels to differ from after_frame_id, 'stable' waits for the screen "
            "to stop changing. Change and stability are heuristics, not proof that an "
            "application is ready."
        ),
    )
    async def vm_wait(
        vm_id: str,
        after_frame_id: str | None = None,
        mode: str = "delay",
        timeout_ms: int = 2000,
        delay_ms: int | None = None,
        threshold: float = 0.005,
    ) -> Annotated[CallToolResult, WaitResult]:
        try:
            result, image = await service.wait(
                vm_id,
                after_frame_id=after_frame_id,
                mode=mode,
                timeout_ms=timeout_ms,
                delay_ms=delay_ms,
                threshold=threshold,
            )
        except PaletteError as exc:
            return _failure(WaitResult, exc)
        return _success(result, image=image)

    @server.tool(
        name="vm_release_inputs",
        title="Let go of everything",
        annotations=CONTROL,
        description=(
            "Release every key and pointer button this service is holding on the VM. Use "
            "it after an error or a cancellation. If the release cannot be confirmed the "
            "result says so instead of claiming success."
        ),
    )
    async def vm_release_inputs(
        vm_id: str, instance_id: str
    ) -> Annotated[CallToolResult, ReleaseResult]:
        try:
            result = await service.release_inputs(vm_id, instance_id=instance_id)
        except PaletteError as exc:
            return _failure(ReleaseResult, exc)
        return _success(result, is_error=not result.ok)


def register_lifecycle_tools(server: MCPServer, service: PaletteService) -> None:
    """Expose lifecycle tools. Their presence is not permission: each call is gated by
    the trusted profile, so an agent cannot authorise itself by calling the tool."""

    @server.tool(
        name="vm_start",
        title="Start the VM",
        annotations=CONTROL,
        description=(
            "Create a new instance from an allowed profile and boot it. A caller may also "
            "name the base image, attach CD-ROMs or extra disks from host paths, and append "
            "raw QEMU arguments; each of those needs its own switch in the profile, and the "
            "last one hands the host to the caller. Operators use the CLI."
        ),
    )
    async def vm_start(
        vm_id: str,
        profile_id: str | None = None,
        image: str | None = None,
        image_format: str | None = None,
        iso: list[str] | None = None,
        extra_disks: list[str] | None = None,
        qemu_args: list[str] | None = None,
    ) -> Annotated[CallToolResult, LifecycleResult]:
        try:
            result = await service.start(
                vm_id,
                profile_id=profile_id,
                actor="agent",
                image=image,
                image_format=image_format,
                iso=iso,
                extra_disks=extra_disks,
                qemu_args=qemu_args,
            )
        except PaletteError as exc:
            return _failure(LifecycleResult, exc, vm_id=vm_id)
        return _success(result)

    @server.tool(
        name="vm_stop",
        title="Stop the VM",
        annotations=CONTROL,
        description=(
            "Ask the guest to shut down, then stop QEMU within the configured budget. "
            "'force' kills the process and is a separate permission."
        ),
    )
    async def vm_stop(
        vm_id: str, force: bool = False
    ) -> Annotated[CallToolResult, LifecycleResult]:
        try:
            result = await service.stop(vm_id, force=force, actor="agent")
        except PaletteError as exc:
            return _failure(LifecycleResult, exc, vm_id=vm_id)
        return _success(result)

    @server.tool(
        name="vm_reset",
        title="Reset the VM",
        annotations=DESTRUCTIVE,
        description=(
            "Throw away the current writable layer and rebuild from the read-only base "
            "image. Destructive: everything the guest had is discarded. Refused unless "
            "the profile grants agents this permission."
        ),
    )
    async def vm_reset(
        vm_id: str, profile_id: str | None = None
    ) -> Annotated[CallToolResult, LifecycleResult]:
        try:
            result = await service.reset(vm_id, profile_id=profile_id, actor="agent")
        except PaletteError as exc:
            return _failure(LifecycleResult, exc, vm_id=vm_id)
        return _success(result)


def register_guest_tools(server: MCPServer, service: PaletteService) -> None:
    """Register the retained guest enhancement tools.

    They are registered only when a profile enables the channel, and every call still
    re-checks the profile grant and the live capability handshake — a cached tool list is
    never treated as an authorization.
    """

    @server.tool(
        name="vm_guest_input_text",
        title="Type text inside the guest",
        annotations=CONTROL,
        description=(
            "Hand text to the in-guest service, which writes it through its own platform "
            "API. Requires an enabled guest tool channel and an explicitly chosen method. "
            "There is no silent fallback to simulated keystrokes."
        ),
    )
    async def vm_guest_input_text(
        vm_id: str,
        instance_id: str,
        boot_epoch: int,
        frame_id: str,
        request_id: str,
        text: str,
        method: str,
        target: str = "focused",
    ) -> Annotated[CallToolResult, GuestToolResult]:
        try:
            result, image = await service.guest_input_text(
                vm_id,
                instance_id=instance_id,
                boot_epoch=boot_epoch,
                frame_id=frame_id,
                request_id=request_id,
                text=text,
                method=method,
                target=target,
            )
        except PaletteError as exc:
            return _failure(GuestToolResult, exc, request_id=request_id)
        return _success(result, image=image, is_error=not result.ok)

    @server.tool(
        name="vm_guest_launch_app",
        title="Launch a registered application",
        annotations=CONTROL,
        description=(
            "Launch an application by its registered app_id in the guest's interactive "
            "desktop. Only apps present in the profile's registry are accepted. A running "
            "process does not prove a window appeared - observe to confirm."
        ),
    )
    async def vm_guest_launch_app(
        vm_id: str,
        instance_id: str,
        boot_epoch: int,
        request_id: str,
        app_id: str,
        options: dict[str, Any] | None = None,
    ) -> Annotated[CallToolResult, GuestToolResult]:
        try:
            result, image = await service.guest_launch_app(
                vm_id,
                instance_id=instance_id,
                boot_epoch=boot_epoch,
                request_id=request_id,
                app_id=app_id,
                options=options or {},
            )
        except PaletteError as exc:
            return _failure(GuestToolResult, exc, request_id=request_id)
        return _success(result, image=image, is_error=not result.ok)


# --------------------------------------------------------------------------------------
# result plumbing
# --------------------------------------------------------------------------------------


def _success(model: Any, *, image: bytes | None = None, is_error: bool = False) -> CallToolResult:
    body = model.model_dump(mode="json")
    content: list[Any] = []
    if image is not None:
        content.append(
            ImageContent(
                type="image",
                data=base64.b64encode(image).decode("ascii"),
                mime_type="image/png",
            )
        )
    content.append(
        TextContent(
            type="text",
            text=json.dumps(body, indent=2, ensure_ascii=False, default=str),
        )
    )
    return CallToolResult(content=content, structured_content=body, is_error=is_error)


def _failure(model: Any, exc: PaletteError, **overrides: Any) -> CallToolResult:
    if exc.code is ErrorCode.INTERNAL_ERROR:
        logger.error("internal failure surfaced to the agent: %s", exc.message)
    fields: dict[str, Any] = {"ok": False, "error": exc.as_payload(), **overrides}
    try:
        payload = model(**fields)
    except Exception:
        # Never let a schema mismatch hide the real failure: fall back to a constructed
        # instance, which every tool's schema accepts because `ok` and `error` are always
        # present in the output model.
        payload = model.model_construct(**fields)
    return _success(payload, is_error=True)
