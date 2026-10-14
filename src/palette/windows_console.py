"""Cancellable cooked console reads without sharing a thread-pool worker."""

from __future__ import annotations

import asyncio
import ctypes
import threading
from ctypes import wintypes
from typing import Any

import anyio


class WindowsConsoleInput:
    def __init__(self, handle: int) -> None:
        self.handle = handle
        win_dll: Any = getattr(ctypes, "WinDLL", None)
        self.api: Any = win_dll("kernel32", use_last_error=True)
        self.api.ReadConsoleW.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        ]
        self.api.ReadConsoleW.restype = wintypes.BOOL
        self.api.GetCurrentThreadId.argtypes = []
        self.api.GetCurrentThreadId.restype = wintypes.DWORD
        self.api.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.api.OpenThread.restype = wintypes.HANDLE
        self.api.CancelSynchronousIo.argtypes = [wintypes.HANDLE]
        self.api.CancelSynchronousIo.restype = wintypes.BOOL
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.restype = wintypes.BOOL

    @staticmethod
    def _error() -> OSError:
        win_error: Any = getattr(ctypes, "WinError", OSError)
        return win_error(getattr(ctypes, "get_last_error", lambda: 0)())  # type: ignore[no-any-return]

    async def read(self, stopping: asyncio.Event) -> str:
        if stopping.is_set():
            return ""
        done = threading.Event()
        completed = asyncio.Event()
        loop = asyncio.get_running_loop()
        thread_handle: int | None = None
        text = ""
        error: BaseException | None = None

        def read_console() -> None:
            nonlocal thread_handle, text, error
            try:
                # Pin this dedicated worker's identity before entering its only read.
                # THREAD_TERMINATE is required by CancelSynchronousIo.
                thread_handle = self.api.OpenThread(1, False, self.api.GetCurrentThreadId())
                if not thread_handle:
                    raise self._error()
                buffer = ctypes.create_unicode_buffer(4096)
                count = wintypes.DWORD()
                if not self.api.ReadConsoleW(
                    self.handle, buffer, len(buffer), ctypes.byref(count), None
                ):
                    raise self._error()
                # Windows WCHARs are UTF-16 code units, including surrogate pairs.
                text = ctypes.string_at(buffer, count.value * 2).decode("utf-16-le", "replace")
            except BaseException as exc:
                error = exc
            finally:
                done.set()
                loop.call_soon_threadsafe(completed.set)

        worker = threading.Thread(target=read_console, name="palette-console-input", daemon=True)
        worker.start()
        read_done = asyncio.create_task(completed.wait())
        stop_requested = asyncio.create_task(stopping.wait())
        try:
            await asyncio.wait({read_done, stop_requested}, return_when=asyncio.FIRST_COMPLETED)
            if stopping.is_set():
                return ""
            if error is not None:
                raise error
            return text
        finally:
            # Cancellation can race with the worker entering ReadConsoleW. Retry until
            # the read completes, then join before its console/thread handles are closed.
            with anyio.CancelScope(shield=True):
                read_done.cancel()
                stop_requested.cancel()
                await asyncio.gather(read_done, stop_requested, return_exceptions=True)
                while not done.is_set():
                    if thread_handle is not None:
                        self.api.CancelSynchronousIo(thread_handle)
                    await anyio.sleep(0.025)
                await anyio.to_thread.run_sync(worker.join)
                if thread_handle is not None:
                    self.api.CloseHandle(thread_handle)
