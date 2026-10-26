"""Private guest transport, negotiated capabilities and bounded correlated requests."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..config import GuestToolsConfig
from ..errors import ErrorCode, PaletteError
from ..guest_channel import GuestChannel
from ..models import EffectState
from .base import GuestCapabilities, GuestToolOutcome
from .protocol import (
    LEGACY_CAPABILITIES,
    OPERATION_CAPABILITIES,
    PROTOCOL_VERSION,
    SUPPORTED_VERSIONS,
    Envelope,
    FrameDecoder,
    HelloAckPayload,
    HelloPayload,
    InputTextParams,
    LaunchAppParams,
    MessageKind,
    ResponsePayload,
    encode_frame,
    negotiate_version,
    parse_envelope,
)

logger = logging.getLogger("palette.guest_tools")


# --------------------------------------------------------------------------------------
# transports
# --------------------------------------------------------------------------------------


@runtime_checkable
class GuestToolTransport(Protocol):
    """A byte channel to the guest service, instance-bound.

    Deliberately not TCP: the plan requires a private, per-instance channel and rejects
    "an unauthenticated listening socket" as a substitute for one.
    """

    @property
    def online(self) -> bool: ...
    def describe(self) -> str: ...
    async def open(self) -> None: ...
    async def send(self, data: bytes) -> None: ...
    async def receive(self, timeout: float) -> bytes: ...
    async def close(self) -> None: ...


class VirtioSerialGuestToolTransport:
    """Instance-private ``org.palette.tools.0`` Windows pipe or Unix socket stream."""

    def __init__(
        self, *, port_name: str = "org.palette.tools.0", endpoint: str | None = None
    ) -> None:
        self.port_name = port_name
        self.endpoint = endpoint
        self._online = False
        self._channel = GuestChannel(endpoint) if endpoint else None
        self._write_lock = asyncio.Lock()

    @property
    def online(self) -> bool:
        return self._online and self._channel is not None and self._channel.connected

    def describe(self) -> str:
        return f"virtio-serial {self.port_name}"

    async def open(self) -> None:
        if self._channel is None:
            raise PaletteError(
                ErrorCode.GUEST_TOOL_UNAVAILABLE,
                "the VM has no private guest tool endpoint",
                details={"port": self.port_name, "phase": "enhancement"},
            )
        if not self._online:
            await self._channel.close()
        await self._channel.open()
        self._online = True

    async def send(self, data: bytes) -> None:
        if not self.online or self._channel is None:
            raise ConnectionError("guest tool channel is not open")
        async with self._write_lock:
            await self._channel.write(data)

    async def receive(self, timeout: float) -> bytes:
        if not self.online or self._channel is None:
            return b""
        try:
            return await self._channel.read(timeout)
        except TimeoutError:
            return b""
        except (OSError, EOFError):
            self._online = False
            raise

    async def close(self) -> None:
        self._online = False
        if self._channel is not None:
            await self._channel.close()


class MemoryGuestToolTransport:
    """In-process transport driven by a responder, used by contract tests.

    It behaves like a real channel: it frames messages, delivers them asynchronously and
    can be told to stall, disconnect or reply out of order, so the bridge's timeout,
    correlation and generation logic is exercised for real.
    """

    def __init__(
        self,
        responder: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
        *,
        latency: float = 0.0,
        fail_on_open: bool = False,
    ) -> None:
        self._responder = responder
        self._inbound: deque[bytes] = deque()
        self._event = asyncio.Event()
        self._online = False
        self._latency = latency
        self._fail_on_open = fail_on_open
        self.sent: list[dict[str, Any]] = []

    @property
    def online(self) -> bool:
        return self._online

    def describe(self) -> str:
        return "in-memory guest tool channel (test double)"

    async def open(self) -> None:
        if self._fail_on_open:
            raise ConnectionError("simulated transport failure")
        self._online = True

    async def send(self, data: bytes) -> None:
        if not self._online:
            raise ConnectionError("guest tool channel is closed")
        for message in FrameDecoder().feed(data):
            self.sent.append(message)
            if self._responder is None:
                continue
            reply = self._responder(message)
            if reply is None:
                continue
            if self._latency:
                await asyncio.sleep(self._latency)
            self._inbound.append(encode_frame(reply))
            self._event.set()

    async def receive(self, timeout: float) -> bytes:
        if not self._online:
            return b""
        if not self._inbound:
            self._event.clear()
            try:
                await asyncio.wait_for(self._event.wait(), timeout=timeout)
            except TimeoutError:
                return b""
        if not self._inbound:
            return b""
        return self._inbound.popleft()

    async def inject(self, payload: dict[str, Any]) -> None:
        self._inbound.append(encode_frame(payload))
        self._event.set()

    async def disconnect(self) -> None:
        self._online = False
        self._event.set()

    async def close(self) -> None:
        self._online = False
        self._event.set()


# --------------------------------------------------------------------------------------
# bridge
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class _Pending:
    future: asyncio.Future[dict[str, Any]]
    capability: str


@dataclass
class BridgeGuestToolBackend:
    """Host side of the guest enhancement channel.

    ``capabilities()`` reports what is *actually* true right now. A handshake that failed,
    a transport that is down, or a guest that advertises nothing all produce an
    unavailable status rather than an optimistic one, because the plan makes a client
    check the result before choosing a fallback itself.
    """

    transport: GuestToolTransport
    config: GuestToolsConfig
    vm_id: str
    instance_id: str
    boot_epoch: int
    _generation: int = 0
    _reader: asyncio.Task[None] | None = None
    _pending: dict[str, _Pending] = field(default_factory=dict)
    _decoder: FrameDecoder = field(default_factory=FrameDecoder)
    _negotiated: str | None = None
    _guest_caps: tuple[str, ...] = ()
    _guest_methods: tuple[str, ...] = ()
    _guest_apps: tuple[str, ...] = ()
    _service_online: bool = False
    _desktop_ready: bool | None = None
    _detail: str | None = None
    _closed: bool = False
    _connect_id: str = ""

    # -- status ----------------------------------------------------------------------
    async def capabilities(self) -> GuestCapabilities:
        transport_online = self.transport.online
        if not transport_online and not self._closed:
            self._service_online = False
        return GuestCapabilities(
            enabled=self.config.enabled,
            transport_online=transport_online,
            service_online=self._service_online and transport_online,
            interactive_desktop_ready=self._desktop_ready,
            protocol_version=self._negotiated,
            connection_generation=self._generation,
            capabilities=self._granted_capabilities(),
            input_methods=self._granted_methods(),
            apps=tuple(sorted(set(self._guest_apps) & set(self.config.allowed_apps))),
            detail=self._detail,
        )

    def _granted_capabilities(self) -> tuple[str, ...]:
        """Intersection of what the profile grants and what the guest advertises.

        Advertising is not permission: a guest claiming a capability the profile never
        granted is treated as if it had not claimed it at all.
        """
        if not self._service_online:
            return ()
        granted = set(self.config.capabilities) & set(self._guest_caps)
        if self._negotiated == "1.0":
            granted &= LEGACY_CAPABILITIES
        return tuple(sorted(granted))

    def _granted_methods(self) -> tuple[str, ...]:
        if not self._service_online:
            return ()
        return tuple(sorted(set(self.config.input_methods) & set(self._guest_methods)))

    # -- lifecycle -------------------------------------------------------------------
    async def connect(self) -> GuestCapabilities:
        """Open the channel and complete the handshake. Never raises for a missing guest
        service: an unavailable enhancement path is a reportable state, not a crash."""
        if not self.config.enabled:
            self._detail = "guest enhancement tools are disabled for this profile"
            return await self.capabilities()

        await self._teardown_reader()
        self._closed = False
        self._generation += 1
        self._negotiated = None
        self._service_online = False
        try:
            async with asyncio.timeout(self.config.request_timeout_seconds):
                await self.transport.open()
        except Exception as exc:
            self._detail = f"guest tool transport unavailable: {type(exc).__name__}"
            logger.info("guest tool transport unavailable: %s", exc)
            return await self.capabilities()

        self._decoder = FrameDecoder(max_bytes=self.config.max_message_bytes)
        self._reader = asyncio.create_task(self._read_loop(), name="guest-tools-reader")
        connect_id = f"conn_{uuid.uuid4().hex[:12]}"
        self._connect_id = connect_id
        hello = Envelope(
            # The unchanged bootstrap envelope is readable by a strict 1.0 peer;
            # subsequent requests use the version selected from payload.versions.
            v="1.0",
            kind=MessageKind.HELLO.value,
            id=connect_id,
            payload=HelloPayload(
                versions=self._supported_versions(),
                vm_id=self.vm_id,
                instance_id=self.instance_id,
                boot_epoch=self.boot_epoch,
                connect_id=connect_id,
                desired_capabilities=sorted(self.config.capabilities),
            ).model_dump(),
        )
        try:
            reply = await self._request(
                hello,
                capability="hello",
                timeout=self.config.request_timeout_seconds,
            )
        except PaletteError as exc:
            self._detail = f"handshake failed: {exc.code}"
            await self._teardown_reader()
            return await self.capabilities()

        try:
            ack = HelloAckPayload.model_validate(reply.get("payload", {}))
        except Exception:
            self._detail = "handshake failed: the guest reply was not a valid hello_ack"
            await self._teardown_reader()
            return await self.capabilities()

        try:
            self._negotiated = negotiate_version(self._supported_versions(), [ack.version])
        except PaletteError as exc:
            self._detail = f"protocol version mismatch: {exc.details}"
            await self._teardown_reader()
            return await self.capabilities()

        if ack.session != self.config.target_session or ack.session != "interactive":
            self._detail = "guest service is not attached to the interactive user session"
            await self._teardown_reader()
            return await self.capabilities()

        self._guest_caps = tuple(ack.capabilities)
        self._guest_methods = tuple(ack.input_methods)
        self._guest_apps = tuple(ack.apps)
        self._desktop_ready = ack.interactive_desktop
        self._service_online = True
        self._detail = ack.detail
        return await self.capabilities()

    async def invalidate(self, *, reason: str = "guest rebooted") -> None:
        """Drop everything tied to the previous boot generation.

        Outstanding requests are failed, not silently awaited: a reply from the previous
        boot must never be matched to a request issued after the reboot.
        """
        self._generation += 1
        for request_id, pending in list(self._pending.items()):
            self._pending.pop(request_id, None)
            if not pending.future.done():
                pending.future.set_exception(
                    PaletteError(
                        ErrorCode.GUEST_TOOL_UNAVAILABLE,
                        f"the guest tool request was abandoned: {reason}",
                    )
                )
                with contextlib.suppress(BaseException):
                    pending.future.exception()
        self._service_online = False
        self._negotiated = None
        self._guest_caps = ()
        self._guest_methods = ()
        self._guest_apps = ()
        self._detail = reason
        await self._teardown_reader()

    async def close(self) -> None:
        self._closed = True
        await self.invalidate(reason="the VM is stopping")
        await self.transport.close()

    # -- operations ------------------------------------------------------------------
    async def execute_operation(
        self,
        operation: str,
        parameters: dict[str, Any],
        *,
        on_sending: Callable[[], None] | None = None,
    ) -> GuestToolOutcome:
        capability = OPERATION_CAPABILITIES.get(operation)
        if capability is None:
            raise PaletteError(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                "unregistered guest operation",
                details={"operation": operation},
            )
        caps = await self.capabilities()
        self._require_ready(caps, capability)
        if caps.protocol_version != "1.1":
            raise PaletteError(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                "this guest operation requires protocol 1.1",
            )
        params = dict(parameters)
        if operation == "resolve_elements":
            params["operation"] = operation
        elif operation.startswith("clipboard."):
            params["operation"] = operation.split(".", 1)[1]
        reply = await self._request(
            self._build_envelope(MessageKind.REQUEST, capability, params),
            capability=capability,
            timeout=self.config.request_timeout_seconds,
            on_sending=on_sending,
        )
        return _outcome_from_reply(reply, capability=capability)

    async def input_text(
        self,
        *,
        text: str,
        method: str,
        target: str = "focused",
        on_sending: Callable[[], None] | None = None,
    ) -> GuestToolOutcome:
        caps = await self.capabilities()
        self._require_ready(caps, "guest.input_text")
        if len(text) > self.config.max_text_chars:
            raise PaletteError(
                ErrorCode.INVALID_ARGUMENT,
                f"text exceeds the {self.config.max_text_chars} character guest limit",
                details={"length": len(text)},
            )
        if method not in caps.input_methods:
            # No transparent downgrade: the caller is told which methods exist and
            # decides for itself whether another one is acceptable.
            raise PaletteError(
                ErrorCode.INPUT_TARGET_UNSUPPORTED,
                f"the requested input method {method!r} is not available",
                details={"available_methods": list(caps.input_methods)},
            )
        params = InputTextParams(text=text, method=method, target=target)
        reply = await self._request(
            self._build_envelope(MessageKind.REQUEST, "guest.input_text", params.model_dump()),
            capability="guest.input_text",
            timeout=self.config.request_timeout_seconds,
            on_sending=on_sending,
        )
        return _outcome_from_reply(reply, capability="guest.input_text")

    async def launch_app(
        self,
        *,
        app_id: str,
        options: dict[str, Any],
        on_sending: Callable[[], None] | None = None,
    ) -> GuestToolOutcome:
        caps = await self.capabilities()
        self._require_ready(caps, "guest.launch_app")
        if app_id not in self.config.allowed_apps:
            raise PaletteError(
                ErrorCode.APP_NOT_ALLOWED,
                f"application {app_id!r} is not registered for this profile",
                details={"allowed_apps": sorted(self.config.allowed_apps)},
            )
        if app_id not in caps.apps:
            raise PaletteError(
                ErrorCode.APP_NOT_ALLOWED,
                f"the guest service does not offer an application named {app_id!r}",
                details={"guest_apps": list(caps.apps)},
            )
        params = LaunchAppParams(app_id=app_id, options=dict(options))
        reply = await self._request(
            self._build_envelope(MessageKind.REQUEST, "guest.launch_app", params.model_dump()),
            capability="guest.launch_app",
            timeout=self.config.request_timeout_seconds,
            on_sending=on_sending,
        )
        return _outcome_from_reply(reply, capability="guest.launch_app")

    def _require_ready(self, caps: GuestCapabilities, capability: str) -> None:
        if not caps.enabled:
            raise PaletteError(
                ErrorCode.GUEST_TOOL_UNAVAILABLE,
                "guest enhancement tools are disabled for this profile",
            )
        if not caps.usable:
            raise PaletteError(
                ErrorCode.GUEST_TOOL_UNAVAILABLE,
                "the guest tool service is not online",
                details={"detail": caps.detail, "generation": caps.connection_generation},
            )
        if capability not in caps.capabilities:
            raise PaletteError(
                ErrorCode.UNSUPPORTED_CAPABILITY,
                f"the guest does not offer {capability!r}",
                details={"granted": list(caps.capabilities)},
            )
        if (
            capability
            in {
                "guest.input_text",
                "guest.launch_app",
                "guest.app",
                "guest.snapshot",
                "guest.batch",
                "guest.clipboard",
            }
            and caps.interactive_desktop_ready is not True
        ):
            raise PaletteError(
                ErrorCode.GUEST_TOOL_UNAVAILABLE, "interactive guest desktop is not ready"
            )

    # -- internals -------------------------------------------------------------------
    def _supported_versions(self) -> list[str]:
        return ["1.0"] if self.config.protocol_version == "1.0" else list(SUPPORTED_VERSIONS)

    def _build_envelope(
        self, kind: MessageKind, capability: str, params: dict[str, Any]
    ) -> Envelope:
        payload: dict[str, Any] = {
            "capability": capability,
            "params": params,
            "session": self.config.target_session,
            "boot_epoch": self.boot_epoch,
            "generation": self._generation,
        }
        if self._negotiated == "1.1":
            payload.update(
                connect_id=self._connect_id,
                timeout_ms=min(65000, max(1, int(self.config.request_timeout_seconds * 1000))),
            )
        return Envelope(
            v=self._negotiated or PROTOCOL_VERSION,
            kind=kind.value,
            id=f"req_{uuid.uuid4().hex[:16]}",
            payload=payload,
        )

    async def _cancel_request(self, request_id: str) -> None:
        if self._negotiated != "1.1" or not self.transport.online:
            return
        envelope = Envelope(
            v="1.1",
            kind=MessageKind.CANCEL,
            id=request_id,
            payload={
                "connect_id": self._connect_id,
                "boot_epoch": self.boot_epoch,
                "generation": self._generation,
            },
        )
        # Cancellation is best effort. It never changes an unknown outcome to not_sent.
        with contextlib.suppress(Exception):
            async with asyncio.timeout(min(0.5, self.config.request_timeout_seconds)):
                await self.transport.send(
                    encode_frame(envelope.model_dump(), max_bytes=self.config.max_message_bytes)
                )

    async def _request(
        self,
        envelope: Envelope,
        *,
        capability: str,
        timeout: float,
        on_sending: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        data = encode_frame(envelope.model_dump(), max_bytes=self.config.max_message_bytes)
        if not self.transport.online:
            raise PaletteError(ErrorCode.GUEST_TOOL_UNAVAILABLE, "the guest channel is offline")
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[envelope.id] = _Pending(future=future, capability=capability)
        sending = False
        try:
            async with asyncio.timeout(timeout):
                if on_sending is not None:
                    on_sending()
                sending = True
                await self.transport.send(data)
                reply = await future
                payload = reply.get("payload", {})
                response_data = payload.get("data", {}) if isinstance(payload, dict) else {}
                if (
                    isinstance(response_data, dict)
                    and response_data.get("reconnect_required") is True
                ):
                    self._service_online = False
                return reply
        except asyncio.CancelledError:
            if sending and capability != "hello":
                await self._cancel_request(envelope.id)
            raise
        except Exception as exc:
            if not sending:
                raise
            if capability != "hello":
                await self._cancel_request(envelope.id)
            self._service_online = False
            raise PaletteError(
                ErrorCode.GUEST_TOOL_UNAVAILABLE
                if capability == "hello"
                else ErrorCode.ACTION_OUTCOME_UNKNOWN,
                "the guest request did not complete; its effect is unknown",
                details={"capability": capability, "timeout_seconds": timeout},
                retryable=False,
            ) from exc
        finally:
            self._pending.pop(envelope.id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    async def _read_loop(self) -> None:
        generation = self._generation
        try:
            while not self._closed:
                try:
                    chunk = await self.transport.receive(
                        min(1.0, self.config.request_timeout_seconds)
                    )
                except PaletteError:
                    if not self.transport.online:
                        break
                    continue
                except Exception as exc:
                    logger.info("guest tool channel read failed: %s", exc)
                    break
                if not chunk:
                    if not self.transport.online:
                        break
                    continue
                if generation != self._generation:
                    # A reply from a previous boot generation: discard it rather than
                    # resolve a request that belongs to the current one.
                    continue
                try:
                    messages = self._decoder.feed(chunk)
                except PaletteError:
                    logger.warning("discarding an oversized guest tool frame")
                    break
                for message in messages:
                    self._deliver(message)
        except asyncio.CancelledError:
            raise
        finally:
            self._service_online = False
            for request_id, pending in list(self._pending.items()):
                self._pending.pop(request_id, None)
                if not pending.future.done():
                    pending.future.set_exception(
                        PaletteError(
                            ErrorCode.GUEST_TOOL_UNAVAILABLE,
                            "the guest tool channel closed",
                        )
                    )
                    with contextlib.suppress(BaseException):
                        pending.future.exception()

    def _deliver(self, message: dict[str, Any]) -> None:
        try:
            envelope = parse_envelope(message)
        except PaletteError as exc:
            logger.warning("ignoring unparseable guest tool message: %s", exc.message)
            return
        pending = self._pending.pop(envelope.id, None)
        if pending is None:
            logger.debug("ignoring guest tool message for unknown id %s", envelope.id)
            return
        if not pending.future.done():
            expected = (
                MessageKind.HELLO_ACK.value
                if pending.capability == "hello"
                else MessageKind.RESPONSE.value
            )
            valid_version = (
                envelope.v in self._supported_versions()
                if pending.capability == "hello"
                else envelope.v == self._negotiated
            )
            if envelope.kind not in {expected, MessageKind.ERROR.value} or not valid_version:
                pending.future.set_exception(
                    PaletteError(
                        ErrorCode.GUEST_TOOL_UNAVAILABLE,
                        "the guest reply has an invalid kind or version",
                    )
                )
            else:
                pending.future.set_result(envelope.model_dump())

    async def _teardown_reader(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
            self._reader = None


def _outcome_from_reply(reply: dict[str, Any], *, capability: str) -> GuestToolOutcome:
    payload = reply.get("payload") or {}
    if reply.get("kind") == MessageKind.ERROR.value:
        return GuestToolOutcome(
            ok=False,
            effect_state=EffectState.UNKNOWN,
            detail=str(payload.get("message") or "the guest reported a failure"),
            error=PaletteError(
                ErrorCode.GUEST_TOOL_UNAVAILABLE,
                "the guest tool service reported a failure",
                details={"capability": capability},
                retryable=False,
            ),
        )
    try:
        response = ResponsePayload.model_validate(payload)
    except Exception:
        return GuestToolOutcome(
            ok=False,
            effect_state=EffectState.UNKNOWN,
            detail="the guest sent a result this host could not parse",
            error=PaletteError(
                ErrorCode.GUEST_TOOL_UNAVAILABLE, "invalid guest result", retryable=False
            ),
        )
    state = response.effect_state
    return GuestToolOutcome(
        ok=response.ok and state is not EffectState.UNKNOWN,
        effect_state=state,
        method=response.method,
        launch_id=response.launch_id,
        detail=response.detail,
        data=response.data,
        error=PaletteError(
            ErrorCode.GUEST_TOOL_UNAVAILABLE,
            response.detail or "the guest operation failed",
            retryable=False,
        )
        if not response.ok or state is EffectState.UNKNOWN
        else None,
    )
