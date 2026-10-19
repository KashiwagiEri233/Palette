"""Business interface consumed by MCP, implemented locally or over IPC."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal, Protocol

from .config import PaletteConfig
from .models import (
    GuestToolResult,
    LifecycleResult,
    ObserveResult,
    Receipt,
    ReleaseResult,
    StatusResult,
    VmListResult,
    WaitResult,
)

Actor = Literal["agent", "operator"]


class AgentService(Protocol):
    config: PaletteConfig

    async def list_vms(self) -> VmListResult: ...

    async def status(self, vm_id: str, *, actor: Actor = "agent") -> StatusResult: ...

    async def observe_image(
        self, vm_id: str, *, request_id: str | None = None, max_long_edge: int | None = None
    ) -> tuple[ObserveResult, bytes]: ...

    async def wait(
        self,
        vm_id: str,
        *,
        after_frame_id: str | None,
        mode: str,
        timeout_ms: int,
        delay_ms: int | None = None,
        threshold: float = 0.005,
    ) -> tuple[WaitResult, bytes | None]: ...

    async def act(
        self,
        vm_id: str,
        *,
        instance_id: str,
        frame_id: str,
        request_id: str,
        action: dict[str, Any],
    ) -> tuple[Receipt, bytes | None]: ...

    async def release_inputs(self, vm_id: str, *, instance_id: str) -> ReleaseResult: ...

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
    ) -> tuple[GuestToolResult, bytes | None]: ...

    async def guest_launch_app(
        self,
        vm_id: str,
        *,
        instance_id: str,
        boot_epoch: int,
        request_id: str,
        app_id: str,
        options: dict[str, Any],
    ) -> tuple[GuestToolResult, bytes | None]: ...

    async def guest_enhancement(
        self,
        vm_id: str,
        *,
        instance_id: str,
        boot_epoch: int,
        request_id: str,
        operation: str,
        parameters: dict[str, Any],
    ) -> tuple[GuestToolResult, bytes | None]: ...

    async def start(
        self,
        vm_id: str,
        *,
        profile_id: str | None = None,
        actor: Actor = "operator",
        image: str | None = None,
        image_format: str | None = None,
        iso: Sequence[str] | None = None,
        extra_disks: Sequence[str] | None = None,
        qemu_args: Sequence[str] | None = None,
    ) -> LifecycleResult: ...

    async def stop(
        self,
        vm_id: str,
        *,
        force: bool = False,
        actor: Actor = "operator",
        expected_instance_id: str | None = None,
    ) -> LifecycleResult: ...

    async def reset(
        self, vm_id: str, *, profile_id: str | None = None, actor: Actor = "operator"
    ) -> LifecycleResult: ...
