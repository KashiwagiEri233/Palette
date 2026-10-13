"""Windows named pipes with explicit user ACLs and cancellable overlapped I/O.

Only kernel waits run in threads. Protocol handling and service calls remain on the
owning asyncio loop; no dependency on its private IOCP implementation is required.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
from ctypes import wintypes
from pathlib import Path
from typing import Any, Literal

from .protocol import CONNECT_TIMEOUT, FRAME_TIMEOUT

_WinDLL: Any = getattr(ctypes, "WinDLL", None)
_get_last_error: Any = getattr(ctypes, "get_last_error", lambda: 0)
_WinError: Any = getattr(ctypes, "WinError", OSError)
_INVALID_HANDLE = ctypes.c_void_p(-1).value
_ERROR_IO_PENDING = 997
_ERROR_PIPE_CONNECTED = 535
_ERROR_PIPE_BUSY = 231
_ERROR_PIPE_NOT_CONNECTED = 233
_ACCEPT_RETRY_SECONDS = 0.025
_WAIT_TIMEOUT = 258
_INFINITE = 0xFFFFFFFF


class _Overlapped(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wintypes.BOOL),
    ]


class _Acl(ctypes.Structure):
    _fields_ = [
        ("revision", wintypes.BYTE),
        ("reserved", wintypes.BYTE),
        ("size", wintypes.WORD),
        ("count", wintypes.WORD),
        ("reserved2", wintypes.WORD),
    ]


class _Api:
    def __init__(self) -> None:
        if _WinDLL is None:
            raise OSError("Windows named pipes are unavailable on this platform")
        kernel = _WinDLL("kernel32", use_last_error=True)
        advapi = _WinDLL("advapi32", use_last_error=True)
        ptr = ctypes.c_void_p
        dword_ptr = ctypes.POINTER(wintypes.DWORD)
        handle = wintypes.HANDLE
        boolean = wintypes.BOOL
        dword = wintypes.DWORD
        self.CreateNamedPipe = self._bind(
            kernel,
            "CreateNamedPipeW",
            [wintypes.LPCWSTR, dword, dword, dword, dword, dword, dword, ptr],
            handle,
        )
        self.CreateFile = self._bind(
            kernel,
            "CreateFileW",
            [wintypes.LPCWSTR, dword, dword, ptr, dword, dword, handle],
            handle,
        )
        self.CreateEvent = self._bind(kernel, "CreateEventW", [ptr, boolean, boolean, ptr], handle)
        self.SetEvent = self._bind(kernel, "SetEvent", [handle], boolean)
        self.CloseHandle = self._bind(kernel, "CloseHandle", [handle], boolean)
        self.ConnectNamedPipe = self._bind(kernel, "ConnectNamedPipe", [handle, ptr], boolean)
        self.DisconnectNamedPipe = self._bind(kernel, "DisconnectNamedPipe", [handle], boolean)
        self.ReadFile = self._bind(
            kernel, "ReadFile", [handle, ptr, dword, dword_ptr, ptr], boolean
        )
        self.WriteFile = self._bind(
            kernel, "WriteFile", [handle, ptr, dword, dword_ptr, ptr], boolean
        )
        self.CancelIoEx = self._bind(kernel, "CancelIoEx", [handle, ptr], boolean)
        self.GetOverlappedResult = self._bind(
            kernel,
            "GetOverlappedResult",
            [handle, ptr, dword_ptr, boolean],
            boolean,
        )
        self.WaitForMultipleObjects = self._bind(
            kernel,
            "WaitForMultipleObjects",
            [dword, ptr, boolean, dword],
            dword,
        )
        self.GetCurrentProcess = self._bind(kernel, "GetCurrentProcess", [], handle)
        self.LocalFree = self._bind(kernel, "LocalFree", [ptr], ptr)
        self.CreateDirectory = self._bind(
            kernel, "CreateDirectoryW", [wintypes.LPCWSTR, ptr], boolean
        )
        self.OpenProcessToken = self._bind(
            advapi, "OpenProcessToken", [handle, dword, ptr], boolean
        )
        self.GetTokenInformation = self._bind(
            advapi,
            "GetTokenInformation",
            [handle, dword, ptr, dword, dword_ptr],
            boolean,
        )
        self.ConvertSidToStringSid = self._bind(
            advapi, "ConvertSidToStringSidW", [ptr, ptr], boolean
        )
        self.ConvertStringSidToSid = self._bind(
            advapi,
            "ConvertStringSidToSidW",
            [wintypes.LPCWSTR, ptr],
            boolean,
        )
        self.ConvertSecurityDescriptor = self._bind(
            advapi,
            "ConvertStringSecurityDescriptorToSecurityDescriptorW",
            [wintypes.LPCWSTR, dword, ptr, ptr],
            boolean,
        )
        self.SetFileSecurity = self._bind(
            advapi,
            "SetFileSecurityW",
            [wintypes.LPCWSTR, dword, ptr],
            boolean,
        )
        self.GetNamedSecurityInfo = self._bind(
            advapi,
            "GetNamedSecurityInfoW",
            [wintypes.LPCWSTR, dword, dword, ptr, ptr, ptr, ptr, ptr],
            dword,
        )
        self.GetSecurityDescriptorControl = self._bind(
            advapi,
            "GetSecurityDescriptorControl",
            [ptr, ptr, dword_ptr],
            boolean,
        )
        self.EqualSid = self._bind(advapi, "EqualSid", [ptr, ptr], boolean)
        self.GetAce = self._bind(advapi, "GetAce", [ptr, dword, ptr], boolean)
        self.sid = self._current_sid()

    @staticmethod
    def _bind(library: Any, name: str, arguments: list[Any], result: Any) -> Any:
        function = getattr(library, name)
        function.argtypes = arguments
        function.restype = result
        return function

    def _current_sid(self) -> str:
        token = wintypes.HANDLE()
        if not self.OpenProcessToken(self.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            raise _WinError(_get_last_error())
        try:
            size = wintypes.DWORD()
            self.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
            data = ctypes.create_string_buffer(size.value)
            if not self.GetTokenInformation(token, 1, data, size, ctypes.byref(size)):
                raise _WinError(_get_last_error())
            sid = ctypes.cast(data, ctypes.POINTER(ctypes.c_void_p))[0]
            text = wintypes.LPWSTR()
            if not self.ConvertSidToStringSid(sid, ctypes.byref(text)):
                raise _WinError(_get_last_error())
            try:
                return str(text.value)
            finally:
                self.LocalFree(ctypes.cast(text, ctypes.c_void_p))
        finally:
            self.CloseHandle(token)


_api_instance: _Api | None = None


def _api() -> _Api:
    global _api_instance
    if _api_instance is None:
        _api_instance = _Api()
    return _api_instance


class _Security:
    def __init__(self, api: _Api, *, directory: bool = False) -> None:
        self.api = api
        self.descriptor = ctypes.c_void_p()
        inheritance = "OICI" if directory else ""
        if not api.ConvertSecurityDescriptor(
            f"D:P(A;{inheritance};GA;;;{api.sid})",
            1,
            ctypes.byref(self.descriptor),
            None,
        ):
            raise _WinError(_get_last_error())
        self.attributes = _SecurityAttributes(
            ctypes.sizeof(_SecurityAttributes), self.descriptor, False
        )

    def close(self) -> None:
        self.api.LocalFree(self.descriptor)


def check_private(path: Path, *, owner_only: bool = False) -> None:
    """Reject a foreign owner or any DACL allowing another SID access."""
    api = _api()
    owner, acl, descriptor, expected = (ctypes.c_void_p() for _ in range(4))
    if not api.ConvertStringSidToSid(api.sid, ctypes.byref(expected)):
        raise _WinError(_get_last_error())
    try:
        result = api.GetNamedSecurityInfo(
            str(path),
            1,
            0x5,
            ctypes.byref(owner),
            None,
            ctypes.byref(acl),
            None,
            ctypes.byref(descriptor),
        )
        if result:
            raise _WinError(result)
        if not api.EqualSid(owner, expected):
            raise PermissionError("operator IPC path is owned by another account")
        if owner_only:
            return
        control, revision = wintypes.WORD(), wintypes.DWORD()
        if not api.GetSecurityDescriptorControl(
            descriptor, ctypes.byref(control), ctypes.byref(revision)
        ):
            raise _WinError(_get_last_error())
        if not acl.value or not control.value & 0x1000:
            raise PermissionError("operator IPC path requires a protected user DACL")
        count = ctypes.cast(acl, ctypes.POINTER(_Acl)).contents.count
        for index in range(count):
            ace = ctypes.c_void_p()
            if not api.GetAce(acl, index, ctypes.byref(ace)):
                raise _WinError(_get_last_error())
            address = int(ace.value or 0)
            kind = ctypes.c_ubyte.from_address(address).value
            if kind == 1:  # A deny ACE grants nothing.
                continue
            if kind != 0 or not api.EqualSid(address + 8, expected):
                raise PermissionError("operator IPC path grants access to another account")
    finally:
        if descriptor.value:
            api.LocalFree(descriptor)
        api.LocalFree(expected)


def ensure_private_directory(path: Path) -> None:
    api = _api()
    security = _Security(api, directory=True)
    try:
        if not api.CreateDirectory(str(path), ctypes.byref(security.attributes)):
            error = _get_last_error()
            if error != 183:
                raise _WinError(error)
        check_private(path)
    finally:
        security.close()


def secure_file(path: Path) -> None:
    api = _api()
    check_private(path, owner_only=True)
    security = _Security(api)
    try:
        if not api.SetFileSecurity(str(path), 0x80000004, security.descriptor):
            raise _WinError(_get_last_error())
    finally:
        security.close()


class WindowsChannel:
    def __init__(self, handle: int) -> None:
        self.api = _api()
        self.handle = handle
        self.stop_event = self.api.CreateEvent(None, True, False, None)
        if not self.stop_event:
            self.api.CloseHandle(handle)
            raise _WinError(_get_last_error())
        self._workers: set[asyncio.Task[bytes]] = set()
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    def _transfer(
        self,
        kind: Literal["accept", "read", "write"],
        payload: bytes | int,
        timeout: float | None,
    ) -> bytes:
        api = self.api
        event = api.CreateEvent(None, True, False, None)
        if not event:
            raise _WinError(_get_last_error())
        overlapped = _Overlapped(hEvent=event)
        count = wintypes.DWORD()
        data = ctypes.create_string_buffer(payload)
        pending = False
        try:
            if kind == "accept":
                success = api.ConnectNamedPipe(self.handle, ctypes.byref(overlapped))
            else:
                function = api.ReadFile if kind == "read" else api.WriteFile
                size = len(payload) if isinstance(payload, bytes) else payload
                success = function(
                    self.handle, data, size, ctypes.byref(count), ctypes.byref(overlapped)
                )
            if not success:
                error = _get_last_error()
                if kind == "accept" and error == _ERROR_PIPE_CONNECTED:
                    return b""
                if error != _ERROR_IO_PENDING:
                    raise _WinError(error)
                pending = True
                handles = (wintypes.HANDLE * 2)(event, self.stop_event)
                milliseconds = _INFINITE if timeout is None else max(1, int(timeout * 1000))
                result = api.WaitForMultipleObjects(2, handles, False, milliseconds)
                if result != 0:
                    if result == _WAIT_TIMEOUT:
                        raise TimeoutError("operator pipe I/O timed out")
                    raise OSError("operator pipe I/O was cancelled")
            if not api.GetOverlappedResult(
                self.handle, ctypes.byref(overlapped), ctypes.byref(count), False
            ):
                raise _WinError(_get_last_error())
            pending = False
            if kind == "read":
                if count.value == 0:
                    raise EOFError("operator pipe closed")
                return bytes(data.raw[: count.value])
            if kind == "write" and isinstance(payload, bytes) and count.value != len(payload):
                raise OSError("incomplete operator pipe write")
            return b""
        finally:
            if pending:
                # The buffer and OVERLAPPED must outlive cancellation completion.
                api.CancelIoEx(self.handle, ctypes.byref(overlapped))
                api.GetOverlappedResult(
                    self.handle, ctypes.byref(overlapped), ctypes.byref(count), True
                )
            api.CloseHandle(event)

    async def _io(
        self,
        kind: Literal["accept", "read", "write"],
        payload: bytes | int,
        timeout: float | None,
    ) -> bytes:
        if self._closed:
            raise OSError("operator pipe is closed")
        worker = asyncio.create_task(asyncio.to_thread(self._transfer, kind, payload, timeout))
        self._workers.add(worker)
        worker.add_done_callback(self._workers.discard)
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            self.api.SetEvent(self.stop_event)
            with contextlib.suppress(Exception):
                await asyncio.shield(worker)
            raise

    async def readexactly(self, size: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < size:
            # receive_frame owns the deadline; an operation response may follow a long boot.
            chunks.extend(await self._io("read", size - len(chunks), None))
        return bytes(chunks)

    async def write(self, data: bytes) -> None:
        await self._io("write", data, FRAME_TIMEOUT)

    async def close(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close(), name="operator-pipe-close")
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        self._closed = True
        self.api.SetEvent(self.stop_event)
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self.api.CloseHandle(self.handle)
        self.api.CloseHandle(self.stop_event)


class WindowsListener:
    def __init__(self, address: str) -> None:
        self.address = address
        self._next = self._create(first=True)
        self._closed = False
        self._stopping = asyncio.Event()

    def _create(self, *, first: bool = False) -> WindowsChannel:
        api = _api()
        security = _Security(api)
        try:
            # Byte mode, no remote clients, no inherited handles, and no name takeover.
            flags = 0x00000003 | 0x40000000 | (0x00080000 if first else 0)
            handle = api.CreateNamedPipe(
                self.address,
                flags,
                0x00000008,
                10,
                65536,
                65536,
                0,
                ctypes.byref(security.attributes),
            )
            if handle == _INVALID_HANDLE:
                raise _WinError(_get_last_error())
            return WindowsChannel(int(handle))
        finally:
            security.close()

    async def accept(self) -> WindowsChannel:
        while True:
            if self.is_closed():
                raise OSError("operator listener is closed")
            channel = self._next
            try:
                await channel._io("accept", 0, None)
            except OSError:
                if self.is_closed():
                    raise
                await self._reject_and_wait(channel)
                continue
            # Keep an instance open while the accepted connection is handed to its handler.
            try:
                self._next = self._create()
            except OSError as exc:
                if getattr(exc, "winerror", exc.errno) != _ERROR_PIPE_BUSY:
                    raise
                # Reuse our instance instead of losing the listener when every slot is in use.
                await self._reject_and_wait(channel)
                continue
            return channel

    async def _reject_and_wait(self, channel: WindowsChannel) -> None:
        if not channel.api.DisconnectNamedPipe(channel.handle):
            error = _get_last_error()
            if error != _ERROR_PIPE_NOT_CONNECTED:
                raise _WinError(error)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), _ACCEPT_RETRY_SECONDS)

    def is_closed(self) -> bool:
        return self._closed

    async def close(self) -> None:
        self._closed = True
        self._stopping.set()
        await self._next.close()


async def connect(address: str) -> WindowsChannel:
    api = _api()
    async with asyncio.timeout(CONNECT_TIMEOUT):
        while True:
            handle = api.CreateFile(address, 0xC0000000, 0, None, 3, 0x40000000, None)
            if handle != _INVALID_HANDLE:
                return WindowsChannel(int(handle))
            error = _get_last_error()
            if error != _ERROR_PIPE_BUSY:  # A local open itself does not block.
                raise _WinError(error)
            await asyncio.sleep(0.025)
