"""QMP protocol layer: handshake, id correlation, event fan-out, timeouts.

QMP and MCP are different protocols that happen to share a JSON encoding. Nothing in
this module knows about JSON-RPC, tools or images, and nothing above it is allowed to
send an arbitrary object to QEMU: callers use typed methods, and the set of commands
used by the service is the reviewed allow-list from the plan.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from ..errors import ErrorCode, PaletteError, QmpCommandError, QmpDisconnectedError
from .transport import QmpTransport

logger = logging.getLogger("palette.qmp")

#: QMP error classes that mean "this build cannot do that", used for feature probing.
UNSUPPORTED_QMP_CLASSES = frozenset({"CommandNotFound", "GenericError", "DeviceNotFound"})

EventHandler = Callable[[str, dict[str, Any]], None]
DisconnectHandler = Callable[[BaseException | None], None]

#: Read-only and control commands Palette is allowed to issue. Kept explicit so that
#: adding a passthrough tool would require editing a reviewed list, not just a call site.
ALLOWED_COMMANDS = frozenset(
    {
        "qmp_capabilities",
        "query-version",
        "query-commands",
        "query-qmp-schema",
        "query-status",
        "query-mice",
        "query-cpus-fast",
        "query-block",
        "qom-list",
        "input-send-event",
        "screendump",
        "cont",
        "stop",
        "system_powerdown",
        "system_reset",
        "quit",
    }
)


def qapi_declares_argument(arg_type: Any, by_name: Mapping[Any, Any], wanted: str) -> bool:
    """Whether a QAPI command's ``arg-type`` declares an argument called ``wanted``.

    ``arg-type`` has two spellings in the wild:

    * a **type name** (often a number, because anonymous types are named by index) whose
      members live in their own schema entry — this is what QEMU 11 emits, e.g.
      ``{"name": "screendump", ..., "arg-type": "132"}`` followed by
      ``{"name": "132", "meta-type": "object", "members": [...]}``;
    * an **inline list or dict** of members, which older outputs used.

    A command with no arguments spells it as the builtin ``"0"``, which resolves to nothing
    and answers ``False`` rather than raising.
    """
    if isinstance(arg_type, str):
        target = by_name.get(arg_type)
        members = target.get("members") if isinstance(target, dict) else None
        return _members_contain(members, wanted)
    if isinstance(arg_type, dict):
        return _members_contain(arg_type.get("members"), wanted)
    return _members_contain(arg_type, wanted)


def _members_contain(members: Any, wanted: str) -> bool:
    if not isinstance(members, list):
        return False
    return any(isinstance(member, dict) and member.get("name") == wanted for member in members)


class QmpClient:
    """One QMP session over one transport.

    The reader runs as its own task so that asynchronous events (``STOP``, ``RESET``,
    ``SHUTDOWN``) are consumed even while no command is outstanding; otherwise QEMU's
    pipe would fill and the VM would stall.
    """

    def __init__(
        self,
        transport: QmpTransport,
        *,
        command_timeout: float = 10.0,
        handshake_timeout: float = 10.0,
        event_history: int = 64,
    ) -> None:
        self._transport = transport
        self._command_timeout = command_timeout
        self._handshake_timeout = handshake_timeout
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._event_handlers: list[EventHandler] = []
        self._disconnect_handlers: list[DisconnectHandler] = []
        self._reader_task: asyncio.Task[None] | None = None
        self._greeting_future: asyncio.Future[dict[str, Any]] | None = None
        self._connected = False
        self._closing = False
        self._counter = 0
        self._greeting: dict[str, Any] | None = None
        self._version: dict[str, Any] | None = None
        self._commands: frozenset[str] | None = None
        self.history: deque[tuple[float, str, dict[str, Any]]] = deque(maxlen=event_history)

    # -- lifecycle -------------------------------------------------------------------
    async def start(self) -> dict[str, Any]:
        """Read the greeting and negotiate capabilities."""
        waiter = self._greeting_future
        if waiter is None:
            waiter = asyncio.get_running_loop().create_future()
            self._greeting_future = waiter
        if self._reader_task is None:
            self._reader_task = asyncio.create_task(self._read_loop(), name="qmp-reader")
        try:
            greeting = await asyncio.wait_for(
                asyncio.shield(waiter), timeout=self._handshake_timeout
            )
        except TimeoutError as exc:
            raise PaletteError(
                ErrorCode.QMP_UNAVAILABLE, "QEMU did not send a QMP greeting in time"
            ) from exc
        self._greeting = greeting
        await self.execute("qmp_capabilities", timeout=self._handshake_timeout)
        self._connected = True
        logger.debug("QMP capabilities negotiated: %s", sorted(greeting.get("QMP", {})))
        return greeting

    async def close(self) -> None:
        self._closing = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader_task
            self._reader_task = None
        await self._transport.close()
        self._fail_pending(QmpDisconnectedError("QMP client closed"))

    @property
    def connected(self) -> bool:
        return self._connected and not self._transport.reader.at_eof()

    @property
    def greeting(self) -> dict[str, Any] | None:
        return self._greeting

    @property
    def version(self) -> dict[str, Any] | None:
        return self._version

    @property
    def commands(self) -> frozenset[str] | None:
        return self._commands

    def on_event(self, handler: EventHandler) -> None:
        self._event_handlers.append(handler)

    def on_disconnect(self, handler: DisconnectHandler) -> None:
        self._disconnect_handlers.append(handler)

    # -- commands --------------------------------------------------------------------
    async def execute(
        self,
        command: str,
        arguments: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
        on_sending: Callable[[], None] | None = None,
    ) -> Any:
        """Send one command and await its matching response.

        A timeout removes the pending future and raises ``TIMEOUT``. The command is
        **not** retried: a QMP write that may already have taken effect is exactly the
        case the plan forbids replaying, so the caller decides what to report.
        """
        if command not in ALLOWED_COMMANDS:
            raise PaletteError(
                ErrorCode.POLICY_DENIED,
                f"command {command!r} is not on the reviewed QMP allow-list",
                details={"command": command},
            )
        if not self.connected and command != "qmp_capabilities":
            raise QmpDisconnectedError()

        message_id = self._next_id()
        payload: dict[str, Any] = {"execute": command, "id": message_id}
        if arguments:
            payload["arguments"] = arguments

        future: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
        self._pending[message_id] = future
        effective = self._command_timeout if timeout is None else timeout
        try:
            async with asyncio.timeout(effective):
                await self._transport.send_json(payload, on_sending=on_sending)
                response = await future
        except TimeoutError:
            self._pending.pop(message_id, None)
            raise PaletteError(
                ErrorCode.TIMEOUT,
                f"QMP command {command!r} timed out; its effect is unknown",
                details={"command": command, "timeout_seconds": effective},
            ) from None
        finally:
            self._pending.pop(message_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

        if "error" in response:
            error = response["error"]
            raise QmpCommandError(
                str(error.get("class", "GenericError")),
                str(error.get("desc", "unknown error")),
                command,
            )
        return response.get("return")

    # -- probing ---------------------------------------------------------------------
    async def probe(self) -> dict[str, Any]:
        """Collect the facts ``doctor`` and the launcher report."""
        self._version = await self.execute("query-version")
        commands = await self.execute("query-commands")
        self._commands = frozenset(str(entry.get("name")) for entry in commands or [])
        return {
            "version": self._version,
            "command_count": len(self._commands),
            "supports_screendump": "screendump" in self._commands,
            "supports_input_send_event": "input-send-event" in self._commands,
            "supports_png_screendump": await self._supports_png_screendump(),
        }

    async def _supports_png_screendump(self) -> bool | None:
        """Ask the schema whether ``screendump`` advertises a ``format`` argument.

        The advertised argument only proves the build knows about PNG; whether libpng was
        compiled in is a runtime question the capture backend answers by falling back to
        PPM when the call fails.

        The shape below was copied from ``query-qmp-schema`` on a real QEMU 11.1, not from
        memory: a command's ``arg-type`` is a **type name**, and the arguments live in a
        separate entry whose ``meta-type`` is ``object``. A hand-written double is free to
        get that wrong, and it did — hence ``qapi_declares_argument`` below being a
        separately tested function rather than an inline comprehension.
        """
        try:
            schema = await self.execute("query-qmp-schema", timeout=self._command_timeout * 3)
        except PaletteError:
            return None
        if not isinstance(schema, list):
            return None
        entries = [entry for entry in schema if isinstance(entry, dict)]
        by_name = {entry.get("name"): entry for entry in entries}
        for entry in entries:
            if entry.get("name") == "screendump" and entry.get("meta-type") == "command":
                return qapi_declares_argument(entry.get("arg-type"), by_name, "format")
        return None

    def has_command(self, name: str) -> bool:
        if self._commands is None:
            return True
        return name in self._commands

    # -- reader ----------------------------------------------------------------------
    async def _read_loop(self) -> None:
        failure: BaseException | None = None
        try:
            while True:
                try:
                    line = await self._transport.reader.readline()
                except (asyncio.LimitOverrunError, ValueError) as exc:
                    failure = exc
                    logger.error("QMP frame exceeded the read buffer: %s", exc)
                    break
                except asyncio.CancelledError:
                    raise
                if not line:
                    break
                self._dispatch_line(line)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = exc
            logger.warning("QMP reader stopped: %s", exc)
        finally:
            self._connected = False
            self._fail_pending(QmpDisconnectedError("QMP channel closed"))
            if not self._closing:
                for handler in list(self._disconnect_handlers):
                    try:
                        handler(failure)
                    except Exception:
                        logger.exception("QMP disconnect handler raised")

    def _dispatch_line(self, line: bytes) -> None:
        text = line.strip()
        if not text:
            return
        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("discarding non-JSON QMP line (%d bytes)", len(text))
            return
        if not isinstance(message, dict):
            logger.warning("discarding non-object QMP message")
            return

        if "QMP" in message and "event" not in message:
            greeting_future = self._greeting_future
            if greeting_future is None:
                self._greeting = message
            elif not greeting_future.done():
                greeting_future.set_result(message)
            else:  # pragma: no cover - a second greeting is a protocol violation
                logger.warning("ignoring duplicate QMP greeting")
            return

        if "event" in message:
            name = str(message["event"])
            data = message.get("data") or {}
            self.history.append((time.monotonic(), name, data if isinstance(data, dict) else {}))
            for handler in list(self._event_handlers):
                try:
                    handler(name, data if isinstance(data, dict) else {})
                except Exception:
                    logger.exception("QMP event handler for %s raised", name)
            return

        message_id = message.get("id")
        if message_id is None:
            logger.warning("QMP response without an id was discarded")
            return
        future = self._pending.pop(str(message_id), None)
        if future is None:
            logger.warning("QMP response for unknown id %r was discarded", message_id)
            return
        if not future.done():
            future.set_result(message)

    def _fail_pending(self, error: BaseException) -> None:
        for message_id, future in list(self._pending.items()):
            self._pending.pop(message_id, None)
            if not future.done():
                future.set_exception(error)
                # Mark the exception as observed so that a caller who never awaits it
                # does not produce an "exception was never retrieved" warning.
                with contextlib.suppress(BaseException):
                    future.exception()

    def _next_id(self) -> str:
        self._counter += 1
        return f"{self._transport.name}-{self._counter}"


async def try_commands(client: QmpClient, commands: Iterable[str]) -> dict[str, bool]:
    """Best-effort feature probe used by ``doctor``; never raises for a missing command."""
    results: dict[str, bool] = {}
    for command in commands:
        try:
            await client.execute(command, timeout=5.0)
        except QmpCommandError:
            results[command] = False
        except PaletteError:
            results[command] = False
        else:
            results[command] = True
    return results
