"""Private endpoint discovery and an OS-held lease for one serve per runtime."""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import os
import secrets
import stat
import sys
from pathlib import Path

from pydantic import ValidationError

from ..errors import ErrorCode, PaletteError
from .protocol import Endpoint, decode_json

IS_WINDOWS = sys.platform == "win32"
_getuid = getattr(os, "getuid", lambda: -1)


def _check_private(path: Path, *, directory: bool = False, socket: bool = False) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise PermissionError("operator IPC paths cannot be links or reparse points")
    expected_type = stat.S_ISDIR if directory else stat.S_ISSOCK if socket else stat.S_ISREG
    if not expected_type(info.st_mode):
        raise PermissionError("unexpected operator IPC path type")
    if IS_WINDOWS:
        from .windows import check_private

        check_private(path)
    elif info.st_uid != _getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise PermissionError("operator IPC paths must be private to the current user")


def _private_directory(path: Path) -> None:
    if IS_WINDOWS:
        from .windows import ensure_private_directory

        ensure_private_directory(path)
    else:
        path.mkdir(mode=0o700, exist_ok=True)
    _check_private(path, directory=True)


def _secure_new_file(path: Path) -> None:
    if IS_WINDOWS:
        from .windows import secure_file

        secure_file(path)
    _check_private(path)


class RuntimePaths:
    def __init__(self, runtime_root: Path) -> None:
        self.root = runtime_root.resolve()
        self.directory = self.root / ".operator"
        self.metadata = self.directory / "endpoint.json"
        digest = hashlib.sha256(os.path.normcase(str(self.root)).encode()).hexdigest()[:20]
        self.key = digest
        self.socket_directory: Path | None = None
        if not IS_WINDOWS:
            uid = _getuid()
            temporary = Path("/tmp")  # noqa: S108 - the per-user directory below is private and verified
            candidate = temporary / f"palette-{uid}" / f"{digest}.sock"
            if len(os.fsencode(candidate)) >= 104:
                temporary = Path("/tmp")  # noqa: S108 - private user directory is verified below
            self.socket_directory = temporary / f"palette-{uid}"

    def address(self, generation: str) -> str:
        if IS_WINDOWS:
            return rf"\\.\pipe\palette-{self.key}-{generation}"
        if self.socket_directory is None:
            raise RuntimeError("no Unix socket directory was selected")
        return str(self.socket_directory / f"{self.key}.sock")

    def prepare(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        _private_directory(self.directory)
        if self.socket_directory is not None:
            _private_directory(self.socket_directory)

    def read_endpoint(self) -> Endpoint:
        _check_private(self.directory, directory=True)
        _check_private(self.metadata)
        fd = os.open(self.metadata, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            data = os.read(fd, 4097)
        finally:
            os.close(fd)
        if len(data) > 4096:
            raise ValueError("operator endpoint metadata exceeds its limit")
        endpoint = Endpoint.model_validate(decode_json(data))
        expected_transport = "pipe" if IS_WINDOWS else "unix"
        if endpoint.transport != expected_transport or endpoint.address != self.address(
            endpoint.generation
        ):
            raise ValueError("operator endpoint is not the expected local address")
        if self.socket_directory is not None:
            _check_private(self.socket_directory, directory=True)
            _check_private(Path(endpoint.address), socket=True)
        return endpoint

    def publish(self, endpoint: Endpoint) -> None:
        temporary = self.directory / f"endpoint-{secrets.token_hex(8)}.tmp"
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            _secure_new_file(temporary)
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(endpoint.model_dump_json().encode())
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(fd)
        try:
            os.replace(temporary, self.metadata)
        finally:
            temporary.unlink(missing_ok=True)

    def retire(self, generation: str) -> None:
        # Do not delete a replacement endpoint belonging to another serve generation.
        try:
            _check_private(self.metadata)
            fd = os.open(self.metadata, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                data = os.read(fd, 4097)
            finally:
                os.close(fd)
            if (
                len(data) <= 4096
                and Endpoint.model_validate(decode_json(data)).generation == generation
            ):
                self.metadata.unlink()
        except (OSError, ValueError, ValidationError):
            pass

    def remove_stale_socket(self, generation: str) -> None:
        if self.socket_directory is None:
            return
        path = Path(self.address(generation))
        try:
            _check_private(path, socket=True)
        except FileNotFoundError:
            return
        path.unlink()


class RuntimeLease:
    """A file lock survives endpoint deletion and is released by the OS on process death."""

    def __init__(self, paths: RuntimePaths) -> None:
        self.paths = paths
        self._fd: int | None = None

    def acquire(self) -> None:
        if self._fd is not None:
            return
        self.paths.prepare()
        path = self.paths.directory / "owner.lock"
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            _check_private(path)
            fd = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
        else:
            try:
                _secure_new_file(path)
            except BaseException:
                os.close(fd)
                raise
        try:
            if IS_WINDOWS:
                locker = importlib.import_module("msvcrt")
                locker.locking(fd, locker.LK_NBLCK, 1)
            else:
                locker = importlib.import_module("fcntl")
                locker.flock(fd, locker.LOCK_EX | locker.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise PaletteError(
                ErrorCode.STATE_CONFLICT,
                "another palette serve already owns this runtime; use palette vm commands to connect",
            ) from exc
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        with contextlib.suppress(OSError):
            if IS_WINDOWS:
                locker = importlib.import_module("msvcrt")
                os.lseek(fd, 0, os.SEEK_SET)
                locker.locking(fd, locker.LK_UNLCK, 1)
        os.close(fd)
