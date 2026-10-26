"""Guest tool backends with explicit capability-gated operation categories."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..errors import ErrorCode, PaletteError
from ..models import EffectState
from .protocol import LEGACY_CAPABILITIES, OPERATION_CAPABILITIES, PROTOCOL_VERSION


@dataclass(frozen=True, slots=True)
class GuestCapabilities:
    """The four states the plan insists on reporting separately, rather than collapsing
    them into one vague "available" flag.

    The transport can be up while the service is down; the service can be up while the
    desktop has no interactive session; and a capability can be advertised but not granted
    by the profile.
    """

    enabled: bool = False
    transport_online: bool = False
    service_online: bool = False
    interactive_desktop_ready: bool | None = None
    protocol_version: str | None = None
    connection_generation: int = 0
    capabilities: tuple[str, ...] = ()
    input_methods: tuple[str, ...] = ()
    apps: tuple[str, ...] = ()
    detail: str | None = None

    @property
    def usable(self) -> bool:
        return self.enabled and self.transport_online and self.service_online

    def supports(self, capability: str) -> bool:
        return self.usable and capability in self.capabilities

    def supports_method(self, method: str) -> bool:
        return self.usable and method in self.input_methods


@dataclass(frozen=True, slots=True)
class GuestToolOutcome:
    """The result of one guest-side operation.

    ``effect_state`` reuses the QMP vocabulary so that a receipt is comparable whichever
    backend executed it, and an ACK is explicitly *not* a claim that the application
    reacted.
    """

    ok: bool
    effect_state: EffectState = EffectState.SUBMITTED
    method: str | None = None
    launch_id: str | None = None
    detail: str | None = None
    error: PaletteError | None = None
    data: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class GuestToolBackend(Protocol):
    async def capabilities(self) -> GuestCapabilities: ...
    async def input_text(
        self,
        *,
        text: str,
        method: str,
        target: str = "focused",
        on_sending: Callable[[], None] | None = None,
    ) -> GuestToolOutcome: ...
    async def launch_app(
        self,
        *,
        app_id: str,
        options: dict[str, object],
        on_sending: Callable[[], None] | None = None,
    ) -> GuestToolOutcome: ...
    async def execute_operation(
        self,
        operation: str,
        parameters: dict[str, Any],
        *,
        on_sending: Callable[[], None] | None = None,
    ) -> GuestToolOutcome: ...
    async def close(self) -> None: ...


class DisabledGuestToolBackend:
    """The default backend: honest about being off.

    It never simulates the missing capability with keystrokes. The plan is explicit that a
    failed enhancement call must not be silently downgraded to keyboard input, because the
    agent would then believe its Unicode text arrived verbatim when it did not.
    """

    async def capabilities(self) -> GuestCapabilities:
        return GuestCapabilities(
            enabled=False,
            detail="guest enhancement tools are disabled for this profile",
        )

    async def input_text(
        self,
        *,
        text: str,
        method: str,
        target: str = "focused",
        on_sending: Callable[[], None] | None = None,
    ) -> GuestToolOutcome:
        raise PaletteError(
            ErrorCode.GUEST_TOOL_UNAVAILABLE,
            "guest text input is not enabled for this profile",
            details={"hint": "use vm_act with type_text for US-ASCII keyboard input"},
        )

    async def launch_app(
        self,
        *,
        app_id: str,
        options: dict[str, object],
        on_sending: Callable[[], None] | None = None,
    ) -> GuestToolOutcome:
        raise PaletteError(
            ErrorCode.GUEST_TOOL_UNAVAILABLE,
            "guest application launching is not enabled for this profile",
        )

    async def close(self) -> None:
        return None

    async def execute_operation(
        self,
        operation: str,
        parameters: dict[str, Any],
        *,
        on_sending: Callable[[], None] | None = None,
    ) -> GuestToolOutcome:
        raise PaletteError(
            ErrorCode.GUEST_TOOL_UNAVAILABLE,
            "guest enhancement tools are not enabled for this profile",
        )


@dataclass
class MockGuestToolBackend:
    """In-memory guest service used by contract tests.

    It enforces the same rules a real service would — registered apps only, advertised
    methods only, no cross-backend downgrade — so that tests of the bridge, the policy and
    the receipt shape exercise the real decision points instead of a permissive stub.
    """

    capabilities_available: bool = True
    service_online: bool = True
    transport_online: bool = True
    protocol_version: str | None = PROTOCOL_VERSION
    input_methods: tuple[str, ...] = ("native_text",)
    apps: dict[str, dict[str, object]] = field(default_factory=lambda: {"text-editor": {}})
    interactive_desktop_ready: bool | None = True
    fail_with: PaletteError | None = None
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    operation_data: dict[str, dict[str, Any]] = field(default_factory=dict)

    async def capabilities(self) -> GuestCapabilities:
        return GuestCapabilities(
            enabled=True,
            transport_online=self.transport_online,
            service_online=self.service_online,
            interactive_desktop_ready=self.interactive_desktop_ready,
            protocol_version=self.protocol_version,
            connection_generation=1,
            capabilities=tuple(sorted(LEGACY_CAPABILITIES | set(OPERATION_CAPABILITIES.values())))
            if self.capabilities_available
            else (),
            input_methods=self.input_methods,
            apps=tuple(sorted(self.apps)),
        )

    async def input_text(
        self,
        *,
        text: str,
        method: str,
        target: str = "focused",
        on_sending: Callable[[], None] | None = None,
    ) -> GuestToolOutcome:
        self.calls.append(("input_text", {"length": len(text), "method": method, "target": target}))
        if self.fail_with is not None:
            raise self.fail_with
        caps = await self.capabilities()
        if not caps.usable:
            raise PaletteError(
                ErrorCode.GUEST_TOOL_UNAVAILABLE, "the guest tool service is offline"
            )
        if method not in caps.input_methods:
            raise PaletteError(
                ErrorCode.INPUT_TARGET_UNSUPPORTED,
                f"the guest service does not offer the {method!r} input method",
                details={"available": list(caps.input_methods)},
            )
        if target != "focused":
            raise PaletteError(
                ErrorCode.INPUT_TARGET_UNSUPPORTED,
                "only the currently focused control is supported",
            )
        if on_sending is not None:
            on_sending()
        return GuestToolOutcome(
            ok=True,
            effect_state=EffectState.SUBMITTED,
            method=method,
            detail="the guest service accepted the text; the application may not have consumed it",
        )

    async def launch_app(
        self,
        *,
        app_id: str,
        options: dict[str, object],
        on_sending: Callable[[], None] | None = None,
    ) -> GuestToolOutcome:
        self.calls.append(("launch_app", {"app_id": app_id, "option_keys": sorted(options)}))
        if self.fail_with is not None:
            raise self.fail_with
        caps = await self.capabilities()
        if not caps.usable:
            raise PaletteError(
                ErrorCode.GUEST_TOOL_UNAVAILABLE, "the guest tool service is offline"
            )
        if app_id not in self.apps:
            raise PaletteError(
                ErrorCode.APP_NOT_ALLOWED,
                f"application {app_id!r} is not registered in the guest",
                details={"allowed_apps": sorted(self.apps)},
            )
        if on_sending is not None:
            on_sending()
        return GuestToolOutcome(
            ok=True,
            effect_state=EffectState.SUBMITTED,
            launch_id=f"launch_{app_id}",
            detail="launch accepted; a running process does not prove a window is focused",
        )

    async def execute_operation(
        self,
        operation: str,
        parameters: dict[str, Any],
        *,
        on_sending: Callable[[], None] | None = None,
    ) -> GuestToolOutcome:
        capability = OPERATION_CAPABILITIES.get(operation)
        if capability is None:
            raise PaletteError(ErrorCode.UNSUPPORTED_CAPABILITY, "unregistered guest operation")
        caps = await self.capabilities()
        if not caps.usable:
            raise PaletteError(
                ErrorCode.GUEST_TOOL_UNAVAILABLE, "the guest tool service is offline"
            )
        if caps.protocol_version != PROTOCOL_VERSION or not caps.supports(capability):
            raise PaletteError(ErrorCode.UNSUPPORTED_CAPABILITY, "guest operation is unavailable")
        if self.fail_with is not None:
            raise self.fail_with
        if on_sending is not None:
            on_sending()
        self.calls.append((operation, dict(parameters)))
        return GuestToolOutcome(ok=True, data=dict(self.operation_data.get(operation, {})))

    async def close(self) -> None:
        return None
