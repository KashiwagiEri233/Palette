"""Windows guest tools shared with the embedded agent's enhancement interface."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult

from .contracts import AgentService
from .errors import PaletteError
from .models import GuestToolResult


def register_windows_tools(server: MCPServer, service: AgentService) -> None:
    from .server import CONTROL, READ_ONLY, _failure, _success

    async def call(
        operation: str,
        vm_id: str,
        instance_id: str,
        boot_epoch: int,
        request_id: str,
        *,
        frame_id: str | None = None,
        **parameters: Any,
    ) -> CallToolResult:
        try:
            result, image = await service.guest_enhancement(
                vm_id,
                instance_id=instance_id,
                boot_epoch=boot_epoch,
                request_id=request_id,
                operation=operation,
                parameters={key: value for key, value in parameters.items() if value is not None},
                frame_id=frame_id,
            )
        except PaletteError as exc:
            return _failure(GuestToolResult, exc, request_id=request_id, effect_state="not_sent")
        return _success(result, image=image, is_error=not result.ok)

    @server.tool(
        name="vm_guest_filesystem",
        title="Manage Windows guest files",
        annotations=CONTROL,
        description="Read/write text, copy/move/delete paths, list/search directories or inspect metadata in the Windows guest. Relative paths start at the guest user's Desktop. read offset is a one-based line, limit bounds lines/results. write supports append; copy/move require destination and explicit overwrite; nonempty directory deletion requires recursive. Text is UTF-8 by default, up to 48 KiB per write. Existing QGA file tools transfer binary files. Runs as the ordinary logged-in guest user; never opens host paths.",
    )
    async def filesystem(
        vm_id: str,
        instance_id: str,
        boot_epoch: int,
        request_id: str,
        mode: Literal["read", "write", "copy", "move", "delete", "list", "search", "info"],
        path: str,
        destination: str | None = None,
        content: str | None = None,
        append: bool = False,
        recursive: bool = False,
        overwrite: bool = False,
        pattern: str | None = None,
        offset: int = 1,
        limit: int = 200,
        encoding: Literal["utf-8", "utf-16", "ascii"] = "utf-8",
    ) -> Annotated[CallToolResult, GuestToolResult]:
        return await call(
            "filesystem",
            vm_id,
            instance_id,
            boot_epoch,
            request_id,
            mode=mode,
            path=path,
            destination=destination,
            content=content,
            append=append,
            recursive=recursive,
            overwrite=overwrite,
            pattern=pattern,
            offset=offset,
            limit=limit,
            encoding=encoding,
        )

    @server.tool(
        name="vm_guest_powershell",
        title="Run Windows guest PowerShell",
        annotations=CONTROL,
        description="Execute PowerShell inside the Windows guest as its ordinary interactive user. Returns bounded stdout/stderr, exit code and timeout/truncation flags. Optional cwd is a guest directory; timeout_seconds defaults to 20, maximum 60. Does not elevate or execute on the host. An unknown outcome must not be replayed automatically.",
    )
    async def powershell(
        vm_id: str,
        instance_id: str,
        boot_epoch: int,
        request_id: str,
        command: str,
        timeout_seconds: float = 20,
        cwd: str | None = None,
    ) -> Annotated[CallToolResult, GuestToolResult]:
        return await call(
            "powershell",
            vm_id,
            instance_id,
            boot_epoch,
            request_id,
            command=command,
            timeout_seconds=timeout_seconds,
            cwd=cwd,
        )

    @server.tool(
        name="vm_guest_process",
        title="Manage Windows guest processes",
        annotations=CONTROL,
        description="List Windows guest processes with name filtering and memory/cpu/name sorting, or terminate a process by pid or unambiguous name. kill requires exactly one target. force selects forced termination. Access is limited by the logged-in guest user's permissions.",
    )
    async def process(
        vm_id: str,
        instance_id: str,
        boot_epoch: int,
        request_id: str,
        mode: Literal["list", "kill"],
        name: str | None = None,
        pid: int | None = None,
        sort_by: Literal["memory", "cpu", "name"] = "memory",
        limit: int = 20,
        force: bool = False,
    ) -> Annotated[CallToolResult, GuestToolResult]:
        return await call(
            "process",
            vm_id,
            instance_id,
            boot_epoch,
            request_id,
            mode=mode,
            name=name,
            pid=pid,
            sort_by=sort_by,
            limit=limit,
            force=force,
        )

    @server.tool(
        name="vm_guest_registry",
        title="Manage Windows guest registry",
        annotations=CONTROL,
        description="Read, set, delete or list Windows guest registry values and keys, subject to the logged-in user's rights. Paths use HKCU:\\Software\\... or HKLM:\\... . An empty name selects the default value; omitting name in delete selects a key. Supports String, ExpandString, Binary (byte array), DWord/QWord (unsigned integer), MultiString (string array).",
    )
    async def registry(
        vm_id: str,
        instance_id: str,
        boot_epoch: int,
        request_id: str,
        mode: Literal["get", "set", "delete", "list"],
        path: str,
        name: str | None = None,
        value: str | int | list[str] | list[int] | None = None,
        type: Literal[  # noqa: A002 - public tool parameter
            "String", "ExpandString", "Binary", "DWord", "MultiString", "QWord"
        ] = "String",
    ) -> Annotated[CallToolResult, GuestToolResult]:
        return await call(
            "registry",
            vm_id,
            instance_id,
            boot_epoch,
            request_id,
            mode=mode,
            path=path,
            name=name,
            value=value,
            type=type,
        )

    @server.tool(
        name="vm_guest_app",
        title="Control Windows guest applications",
        annotations=CONTROL,
        description="Launch an app by its localized Start Menu name, launch_executable using a guest executable plus separated args and optional cwd, resize/move a window, or switch focus. Window targets are snapshot window_id or an unambiguous name; omitted target uses the active window. Launch is unrestricted within the current guest user's permissions and does not elevate. Returns a screenshot; verify actual foreground/window behavior.",
    )
    async def app(
        vm_id: str,
        instance_id: str,
        boot_epoch: int,
        request_id: str,
        mode: Literal["launch", "launch_executable", "resize", "switch"] = "launch",
        name: str | None = None,
        executable: str | None = None,
        args: list[str] | None = None,
        cwd: str | None = None,
        window_id: str | None = None,
        window_loc: list[int] | None = None,
        window_size: list[int] | None = None,
    ) -> Annotated[CallToolResult, GuestToolResult]:
        return await call(
            "app",
            vm_id,
            instance_id,
            boot_epoch,
            request_id,
            mode=mode,
            name=name,
            executable=executable,
            args=args,
            cwd=cwd,
            window_id=window_id,
            window_loc=window_loc,
            window_size=window_size,
        )

    @server.tool(
        name="vm_guest_snapshot",
        title="Inspect Windows guest controls",
        annotations=READ_ONLY,
        description="Capture a Windows UI Automation snapshot and QEMU screenshot. Returns snapshot_id, window ids, numbered interactive controls, native guest bounds and frame_id. Use this frame_id and snapshot_id together for label-based multi_select/multi_edit; use image pixel coordinates for locs. Snapshots are bounded (max_elements default 500, maximum 2000) and can be truncated. Locked or unavailable desktops are reported explicitly.",
    )
    async def snapshot(
        vm_id: str,
        instance_id: str,
        boot_epoch: int,
        request_id: str,
        max_elements: int = 500,
    ) -> Annotated[CallToolResult, GuestToolResult]:
        return await call(
            "snapshot", vm_id, instance_id, boot_epoch, request_id, max_elements=max_elements
        )

    @server.tool(
        name="vm_guest_multi_select",
        title="Select Windows guest controls in a batch",
        annotations=CONTROL,
        description="Click up to 64 targets sequentially in one queued transaction, optionally holding Ctrl for multi-selection (default true). Provide exactly one of locs=[[x,y],...] in screenshot pixels or labels=[id,...] plus snapshot_id from vm_guest_snapshot. Requires that snapshot's frame_id for labels, or the newest observed frame for coordinates. Stale/moved controls are rejected. Failure reports completed/total; there is no rollback or automatic replay.",
    )
    async def multi_select(
        vm_id: str,
        instance_id: str,
        boot_epoch: int,
        request_id: str,
        frame_id: str,
        locs: list[list[int]] | None = None,
        labels: list[int] | None = None,
        snapshot_id: str | None = None,
        press_ctrl: bool = True,
    ) -> Annotated[CallToolResult, GuestToolResult]:
        return await call(
            "multi_select",
            vm_id,
            instance_id,
            boot_epoch,
            request_id,
            frame_id=frame_id,
            locs=locs,
            labels=labels,
            snapshot_id=snapshot_id,
            press_ctrl=press_ctrl,
        )

    @server.tool(
        name="vm_guest_multi_edit",
        title="Fill Windows guest fields in a batch",
        annotations=CONTROL,
        description="Fill up to 64 fields sequentially using native Unicode input. Provide exactly one of locs=[[x,y,text],...] in screenshot pixels, or labels=[[id,text],...] plus snapshot_id. Requires the newest frame_id (the snapshot's frame for labels). clear defaults to true (Ctrl+A, Backspace); false appends. Total UTF-8 text is limited to 48 KiB and each field to the profile text limit. Returns screenshot and completed/total; partial/unknown results must be inspected before any retry.",
    )
    async def multi_edit(
        vm_id: str,
        instance_id: str,
        boot_epoch: int,
        request_id: str,
        frame_id: str,
        locs: list[list[Any]] | None = None,
        labels: list[list[Any]] | None = None,
        snapshot_id: str | None = None,
        clear: bool = True,
    ) -> Annotated[CallToolResult, GuestToolResult]:
        return await call(
            "multi_edit",
            vm_id,
            instance_id,
            boot_epoch,
            request_id,
            frame_id=frame_id,
            locs=locs,
            labels=labels,
            snapshot_id=snapshot_id,
            clear=clear,
        )
