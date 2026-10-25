"""Bounded upstream QGA client; no shell, password, or arbitrary QGA command API."""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import secrets
import time
from typing import Any

from .errors import ErrorCode, PaletteError
from .guest_channel import GuestChannel

CHUNK_BYTES = 48 * 1024
_MAX_MESSAGE = 256 * 1024
_COMMANDS = frozenset(
    {
        "guest-set-time",
        "guest-shutdown",
        "guest-file-open",
        "guest-file-read",
        "guest-file-write",
        "guest-file-close",
        "guest-get-host-name",
        "guest-get-osinfo",
        "guest-network-get-interfaces",
    }
)


def _display_metadata(key: str, value: Any) -> Any:
    """Only bounded, typed fields cross from the untrusted guest into the desktop."""
    if key == "hostname":
        name = value.get("host-name") if isinstance(value, dict) else None
        return {"host-name": name[:256]} if isinstance(name, str) else {}
    if key == "os":
        keys = {
            "id",
            "name",
            "pretty-name",
            "version",
            "version-id",
            "kernel-release",
            "kernel-version",
            "machine",
            "variant",
            "variant-id",
        }
        return (
            {
                name: item[:512]
                for name, item in value.items()
                if name in keys and isinstance(item, str)
            }
            if isinstance(value, dict)
            else {}
        )
    interfaces: list[dict[str, Any]] = []
    if isinstance(value, list):
        for interface in value[:64]:
            if not isinstance(interface, dict) or not isinstance(interface.get("name"), str):
                continue
            addresses = interface.get("ip-addresses")
            interfaces.append(
                {
                    "name": interface["name"][:128],
                    "ip-addresses": [
                        {"ip-address": item["ip-address"][:64]}
                        for item in (addresses[:8] if isinstance(addresses, list) else [])
                        if isinstance(item, dict) and isinstance(item.get("ip-address"), str)
                    ],
                }
            )
    return interfaces


class QemuGuestAgent:
    def __init__(
        self, endpoint: str, *, timeout_seconds: float = 3, max_file_bytes: int = 16 << 20
    ) -> None:
        self.channel = GuestChannel(endpoint)
        self.timeout = timeout_seconds
        self.max_file_bytes = max_file_bytes
        self.lock = asyncio.Lock()
        self.online = False
        self.synced = False
        self.generation = 0
        self.info: dict[str, Any] = {}
        self.commands: set[str] = set()
        self.transfers: dict[str, tuple[int, str, int]] = {}
        self.buffer = bytearray()
        self.detail = "客户机服务尚未响应"

    def status(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "online": self.online,
            "commands": sorted(self.commands) if self.online else [],
            "detail": self.detail,
            **(self.info if self.online else {}),
        }

    def invalidate(self) -> None:
        self.generation += 1
        self.online = self.synced = False
        self.commands.clear()
        self.info.clear()
        self.transfers.clear()
        self.buffer.clear()
        self.detail = "正在等待客户机服务重新连接"

    async def _reply(self, *, sync_id: int | None = None) -> Any:
        deadline = time.monotonic() + self.timeout
        scanned = 0
        while True:
            if b"\xff" in self.buffer:
                # A sentinel discards any stale, possibly partial JSON before it.
                self.buffer = self.buffer[self.buffer.rfind(b"\xff") + 1 :]
            if b"\n" in self.buffer:
                line, _, tail = self.buffer.partition(b"\n")
                self.buffer = bytearray(tail)
                try:
                    value = json.loads(line)
                except (ValueError, RecursionError):
                    if sync_id is not None:
                        continue
                    raise ConnectionError("invalid QGA response") from None
                if not isinstance(value, dict):
                    raise ConnectionError("invalid QGA response")
                if sync_id is not None and value.get("return") != sync_id:
                    continue
                if "error" in value:
                    raise PaletteError(ErrorCode.UNSUPPORTED_CAPABILITY, "客户机拒绝了该增强操作")
                if "return" not in value:
                    raise ConnectionError("QGA response is missing return")
                return value["return"]
            if len(self.buffer) > _MAX_MESSAGE or scanned > _MAX_MESSAGE * 2:
                raise ConnectionError("QGA response exceeds limit")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            data = await self.channel.read(remaining)
            scanned += len(data)
            self.buffer.extend(data)

    async def _send(self, command: str, arguments: dict[str, Any] | None = None) -> None:
        data: dict[str, Any] = {"execute": command}
        if arguments is not None:
            data["arguments"] = arguments
        await self.channel.write(json.dumps(data, separators=(",", ":")).encode() + b"\n")

    async def _synchronize(self) -> None:
        await self.channel.open()
        if not self.synced:
            self.buffer.clear()
            nonce = secrets.randbits(53)
            await self.channel.write(b"\xff")
            await self._send("guest-sync-delimited", {"id": nonce})
            await self._reply(sync_id=nonce)
            self.synced = True

    async def refresh_info(self) -> dict[str, Any]:
        async with self.lock:
            generation = self.generation
            try:
                await self._synchronize()
                await self._send("guest-info")
                info = await self._reply()
                if not isinstance(info, dict) or not isinstance(
                    info.get("supported_commands"), list
                ):
                    raise ConnectionError("invalid QGA capabilities")
                self.commands = {
                    item["name"]
                    for item in info["supported_commands"]
                    if isinstance(item, dict)
                    and item.get("enabled") is True
                    and isinstance(item.get("name"), str)
                } & _COMMANDS
                metadata: dict[str, Any] = {"version": str(info.get("version", "unknown"))[:128]}
                for command, key in (
                    ("guest-get-host-name", "hostname"),
                    ("guest-get-osinfo", "os"),
                    ("guest-network-get-interfaces", "interfaces"),
                ):
                    if command in self.commands:
                        await self._send(command)
                        with contextlib.suppress(PaletteError):
                            metadata[key] = _display_metadata(key, await self._reply())
                if generation != self.generation:
                    raise ConnectionError("guest reset during query")
                if len(json.dumps(metadata).encode()) > 64 * 1024:
                    raise ConnectionError("QGA metadata exceeds limit")
                self.info = metadata
                self.online = True
                self.detail = "客户机增强服务在线"
            except asyncio.CancelledError:
                self.online = self.synced = False
                raise
            except (OSError, EOFError, ConnectionError, TimeoutError, PaletteError):
                self.online = self.synced = False
                self.detail = "客户机服务未响应, 基础功能可用"
            return self.status()

    async def _command(
        self, name: str, args: dict[str, Any] | None = None, *, response: bool = True
    ) -> Any:
        if not self.online:
            raise PaletteError(ErrorCode.GUEST_TOOL_UNAVAILABLE, "客户机增强服务不可用")
        if name not in self.commands:
            raise PaletteError(ErrorCode.UNSUPPORTED_CAPABILITY, "客户机不支持该增强功能")
        generation = self.generation
        attempted = False
        try:
            await self._synchronize()
            attempted = True
            await self._send(name, args)
            result = await self._reply() if response else {}
            if generation != self.generation:
                raise ConnectionError("guest reset during operation")
            return result
        except asyncio.CancelledError:
            self.online = self.synced = False
            raise
        except (OSError, EOFError, ConnectionError, TimeoutError) as exc:
            self.online = self.synced = False
            self.detail = "增强通道中断, 请重新检查状态"
            raise PaletteError(
                ErrorCode.ACTION_OUTCOME_UNKNOWN if attempted else ErrorCode.GUEST_TOOL_UNAVAILABLE,
                "增强操作结果未知, 未自动重试" if attempted else "增强通道不可用",
            ) from exc

    async def sync_time(self) -> dict[str, Any]:
        async with self.lock:
            await self._command("guest-set-time", {"time": time.time_ns()})
            return {"synced": True}

    async def shutdown(self) -> None:
        async with self.lock:
            # QGA shutdown deliberately has no success reply. Lifecycle waits for process exit.
            await self._command("guest-shutdown", {"mode": "powerdown"}, response=False)
            self.synced = False

    async def file_open(self, path: str, mode: str) -> dict[str, Any]:
        if (
            mode not in {"r", "w"}
            or not path
            or len(path) > 4096
            or "\0" in path
            or not (path.startswith("/") or (len(path) > 2 and path[1] == ":" and path[2] in "\\/"))
        ):
            raise PaletteError(ErrorCode.INVALID_ARGUMENT, "客户机文件路径或模式无效")
        async with self.lock:
            if len(self.transfers) >= 4:
                raise PaletteError(ErrorCode.BUSY, "同时传输的文件过多")
            handle = await self._command("guest-file-open", {"path": path, "mode": mode + "b"})
            if type(handle) is not int:
                raise PaletteError(ErrorCode.GUEST_TOOL_UNAVAILABLE, "客户机返回了无效文件句柄")
            token = secrets.token_hex(16)
            self.transfers[token] = (handle, mode, 0)
            return {"transfer_id": token, "max_file_bytes": self.max_file_bytes}

    def _transfer(self, token: str, mode: str) -> tuple[int, int]:
        item = self.transfers.get(token)
        if item is None or item[1] != mode:
            raise PaletteError(ErrorCode.INVALID_ARGUMENT, "文件传输已结束或不属于当前客户机")
        return item[0], item[2]

    async def file_read(self, token: str, count: int = CHUNK_BYTES) -> dict[str, Any]:
        if type(count) is not int or not 1 <= count <= CHUNK_BYTES:
            raise PaletteError(ErrorCode.INVALID_ARGUMENT, "文件块大小无效")
        async with self.lock:
            handle, total = self._transfer(token, "r")
            result = await self._command("guest-file-read", {"handle": handle, "count": count})
            try:
                data = base64.b64decode(result["buf-b64"], validate=True)
                if len(data) > count or result["count"] != len(data):
                    raise ValueError
            except (ValueError, KeyError, TypeError, binascii.Error) as exc:
                raise PaletteError(ErrorCode.GUEST_TOOL_UNAVAILABLE, "客户机文件块无效") from exc
            if total + len(data) > self.max_file_bytes:
                raise PaletteError(ErrorCode.INVALID_ARGUMENT, "文件超过传输大小限制")
            self.transfers[token] = (handle, "r", total + len(data))
            return {
                "data_base64": base64.b64encode(data).decode(),
                "count": len(data),
                "eof": bool(result.get("eof")),
            }

    async def file_write(self, token: str, encoded: str) -> dict[str, Any]:
        try:
            if len(encoded) > CHUNK_BYTES * 4 // 3 + 4:
                raise ValueError
            data = base64.b64decode(encoded, validate=True)
            if not 0 < len(data) <= CHUNK_BYTES:
                raise ValueError
        except (ValueError, binascii.Error) as exc:
            raise PaletteError(ErrorCode.INVALID_ARGUMENT, "文件块无效") from exc
        async with self.lock:
            handle, total = self._transfer(token, "w")
            if total + len(data) > self.max_file_bytes:
                raise PaletteError(ErrorCode.INVALID_ARGUMENT, "文件超过传输大小限制")
            result = await self._command("guest-file-write", {"handle": handle, "buf-b64": encoded})
            self.transfers[token] = (handle, "w", total + len(data))
            if not isinstance(result, dict) or result.get("count") != len(data):
                raise PaletteError(
                    ErrorCode.ACTION_OUTCOME_UNKNOWN, "客户机文件未完整写入, 请检查目标文件"
                )
            return {"count": len(data)}

    async def file_close(self, token: str) -> dict[str, Any]:
        async with self.lock:
            item = self.transfers.pop(token, None)
            if item is not None:
                await self._command("guest-file-close", {"handle": item[0]})
            return {}

    async def close_files(self) -> None:
        try:
            # Revoking control must also be bounded while another transfer owns the lock.
            async with asyncio.timeout(self.timeout):
                for token in list(self.transfers):
                    with contextlib.suppress(PaletteError):
                        await self.file_close(token)
        except TimeoutError:
            self.invalidate()

    async def close(self) -> None:
        self.invalidate()
        await self.channel.close()
