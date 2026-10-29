"""Private Windows AF_UNIX guest endpoints, independent of QMP startup.

QEMU's pipe chardev can deadlock if a client beats ConnectNamedPipe. Socket
chardevs with wait=off remove that barrier. CPython's Windows Proactor cannot
connect AF_UNIX sockets, so Winsock establishes them before asyncio takes over.
"""

from __future__ import annotations

import asyncio
import ctypes
import select
import socket
import tempfile
import uuid
from pathlib import Path
from typing import Any

from .errors import ErrorCode, PaletteError

AF_UNIX = 1
PATH_BYTES = 108
_CONNECTING = frozenset({10035, 10036, 10037})
_RETRYABLE = frozenset({2, 3, 10061})
RETRY_SECONDS = 0.025
_WinDLL: Any = getattr(ctypes, "WinDLL", None)


class _Address(ctypes.Structure):
    _fields_ = [("family", ctypes.c_ushort), ("path", ctypes.c_char * PATH_BYTES)]


class _Winsock:
    def __init__(self) -> None:
        if _WinDLL is None:
            raise OSError("Windows AF_UNIX is unavailable on this platform")
        library = _WinDLL("ws2_32")
        self.connect = library.connect
        self.connect.argtypes = [ctypes.c_size_t, ctypes.c_void_p, ctypes.c_int]
        self.connect.restype = ctypes.c_int
        self.last_error = library.WSAGetLastError
        self.last_error.argtypes = []
        self.last_error.restype = ctypes.c_int


_winsock: _Winsock | None = None


def validate_endpoint(endpoint: str) -> bytes:
    encoded = endpoint.encode("utf-8")
    if b"\0" in encoded or len(encoded) >= PATH_BYTES:
        raise PaletteError(
            ErrorCode.PRECONDITION_FAILED,
            "Windows guest socket path must be shorter than 108 UTF-8 bytes; "
            "use a shorter TEMP path",
            details={"phase": "guest-endpoints", "path_bytes": len(encoded)},
        )
    return encoded


def endpoint_directory() -> Path:
    temporary = Path(tempfile.gettempdir())
    if not temporary.is_absolute():
        raise PaletteError(
            ErrorCode.PRECONDITION_FAILED,
            "TEMP must name an absolute directory for private guest sockets",
        )
    # On some Windows hosts AF_UNIX nodes directly under LocalAppData bind but
    # cannot be connected to (WSAEINVAL) or removed (ERROR_CANNOT_ACCESS_FILE).
    # The per-user temporary namespace supports the full socket lifecycle.
    directory = temporary / "Palette" / "ipc" / uuid.uuid4().hex
    validate_endpoint(str(directory / "session.sock"))
    return directory


def require_support() -> None:
    try:
        with socket.socket(AF_UNIX, socket.SOCK_STREAM):
            pass
    except OSError as exc:
        raise PaletteError(
            ErrorCode.UNSUPPORTED_CAPABILITY,
            "Windows AF_UNIX support is required for private QEMU channels",
            details={"phase": "guest-endpoints", "error": str(exc)},
        ) from exc


def prepare_directory(directory: Path) -> None:
    from .operator.paths import _private_directory

    require_support()
    for parent in (directory.parent.parent, directory.parent, directory):
        _private_directory(parent)


def cleanup_directory(directory: Path, endpoints: list[str]) -> None:
    from .operator.paths import _check_private

    if not directory.exists():
        return
    _check_private(directory, directory=True)
    for endpoint in endpoints:
        path = Path(endpoint)
        if path.parent != directory or path.name not in {
            "qmp.sock",
            "qga.sock",
            "tools.sock",
            "webdav.sock",
            "session.sock",
        }:
            raise PermissionError("guest endpoint is outside its instance IPC directory")
        # Socket nodes are reparse points on Windows. Unlink the exact owned nodes;
        # never resolve them or recursively remove their parent.
        path.unlink(missing_ok=True)
    directory.rmdir()


def _connect(connection: socket.socket, address: _Address) -> int:
    global _winsock
    if _winsock is None:
        _winsock = _Winsock()
    result = _winsock.connect(connection.fileno(), ctypes.byref(address), ctypes.sizeof(address))
    return 0 if result == 0 else int(_winsock.last_error())


async def open_connection(
    endpoint: str, *, limit: int = 65536
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Use the caller's deadline; cancellation closes every unhanded socket."""
    address = _Address(family=AF_UNIX, path=validate_endpoint(endpoint))
    while True:
        connection = socket.socket(AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.setblocking(False)
            error = _connect(connection, address)
            if error in _CONNECTING:
                while True:
                    _, writable, failed = select.select([], [connection], [connection], 0)
                    if writable or failed:
                        error = connection.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                        break
                    await asyncio.sleep(RETRY_SECONDS)
            if error:
                raise OSError(error, "Windows guest socket connection failed")
            # Connected sockets bypass Proactor's INET-only ConnectEx. Subsequent
            # WSARecv/WSASend operations support AF_UNIX.
            return await asyncio.open_connection(sock=connection, limit=limit)
        except OSError as exc:
            connection.close()
            if exc.errno not in _RETRYABLE:
                raise
            await asyncio.sleep(RETRY_SECONDS)
        except BaseException:
            connection.close()
            raise
