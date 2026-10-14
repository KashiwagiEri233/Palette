"""MCP stdio input that can end without waiting for the client's pipe to close."""

from __future__ import annotations

import asyncio
import ctypes
import os
import sys
from typing import Any

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.stdio import stdio_server


class StoppableInput(anyio.AsyncFile[str]):
    """Read only available bytes; an idle pipe must never hold a worker thread."""

    def __init__(self, fd: int, stopping: asyncio.Event) -> None:
        self._fd = os.dup(fd)
        super().__init__(os.fdopen(self._fd, "r", encoding="utf-8", errors="replace"))
        self._stopping = stopping
        self._pending = bytearray()
        self._eof = False
        self._peek: Any = None
        self._handle: int | None = None
        self._disk = False
        if sys.platform == "win32":
            import msvcrt
            from ctypes import wintypes

            win_dll: Any = getattr(ctypes, "WinDLL", None)
            kernel: Any = win_dll("kernel32", use_last_error=True)
            self._handle = msvcrt.get_osfhandle(self._fd)
            msvcrt.setmode(self._fd, os.O_BINARY)
            kernel.GetFileType.argtypes = [wintypes.HANDLE]
            kernel.GetFileType.restype = wintypes.DWORD
            file_type = kernel.GetFileType(self._handle)
            kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel.GetConsoleMode.restype = wintypes.BOOL
            mode = wintypes.DWORD()
            self._disk = file_type == 1 or (
                file_type == 2 and not kernel.GetConsoleMode(self._handle, ctypes.byref(mode))
            )
            self._peek = kernel.PeekNamedPipe
            self._peek.argtypes = [
                wintypes.HANDLE,
                ctypes.c_void_p,
                wintypes.DWORD,
                ctypes.c_void_p,
                ctypes.POINTER(wintypes.DWORD),
                ctypes.c_void_p,
            ]
            self._peek.restype = wintypes.BOOL

    def _available(self) -> int:
        if sys.platform != "win32":
            import select

            return 4096 if select.select([self._fd], [], [], 0)[0] else 0
        if self._disk:
            return 4096
        from ctypes import wintypes

        available = wintypes.DWORD()
        if not self._peek(self._handle, None, 0, None, ctypes.byref(available), None):
            error = getattr(ctypes, "get_last_error", lambda: 0)()
            if error in {109, 232, 233}:  # Broken, closing or disconnected pipe.
                self._eof = True
                return 0
            raise OSError(error, "cannot read MCP stdin pipe")
        return min(available.value, 4096)

    async def readline(self) -> str:
        while not self._stopping.is_set():
            newline = self._pending.find(b"\n")
            if newline >= 0 or self._eof:
                size = newline + 1 if newline >= 0 else len(self._pending)
                line = bytes(self._pending[:size]).decode("utf-8", errors="replace")
                del self._pending[:size]
                return line
            size = self._available()
            if size:
                chunk = os.read(self._fd, size)
                self._pending.extend(chunk)
                self._eof = not chunk
            else:
                await anyio.sleep(0.025)
        return ""


class StoppableMCPServer(MCPServer):
    def __init__(self, *, input_stopping: asyncio.Event, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.input_stopping = input_stopping

    async def run_stdio_async(self) -> None:
        fd = sys.stdin.fileno()
        async with StoppableInput(fd, self.input_stopping) as stdin:
            # Preserve the SDK's stdin diversion: helpers inheriting fd 0 see EOF,
            # while only this reader retains the client's private wire duplicate.
            diversion = os.open(os.devnull, os.O_RDONLY)
            try:
                os.dup2(diversion, fd)
            finally:
                os.close(diversion)
            try:
                self._rebind_stdin(fd)
                async with stdio_server(stdin=stdin) as (read_stream, write_stream):
                    await self._lowlevel_server.run(
                        read_stream,
                        write_stream,
                        self._lowlevel_server.create_initialization_options(),
                    )
            finally:
                os.dup2(stdin.wrapped.fileno(), fd)
                self._rebind_stdin(fd)

    @staticmethod
    def _rebind_stdin(fd: int) -> None:
        if sys.platform == "win32":
            from mcp.os.win32.utilities import rebind_std_handle_to_fd

            rebind_std_handle_to_fd(fd)
