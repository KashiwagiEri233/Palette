"""Process guards: make sure a QEMU process cannot outlive the service.

On Windows the guard is a Job Object with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``. The
handle is owned by the service, so if the service dies for any reason — clean exit, crash,
or being killed — the kernel tears the whole job down and no orphaned QEMU keeps running
against the base image.

The Linux implementation is a documented no-op for now; the plan defers it to the KVM
port, where a supervising parent process is the equivalent mechanism. The interface is
already here so the manager code does not change when that lands.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger("palette.vm.guard")

#: A plain bool rather than a direct ``sys.platform`` comparison in each branch: mypy narrows
#: ``sys.platform`` to the *checking* machine's literal value, which makes the other platform's
#: branch look unreachable — and, worse, type-checks that branch against the wrong platform's
#: stubs. CI runs the checker on Linux, so both have to hold.
IS_WINDOWS = sys.platform == "win32"

#: typeshed exposes ``WinDLL`` and ``get_last_error`` only when the machine running the type
#: checker is Windows, so a Linux check reports them missing even though this code cannot
#: execute there. Fetching them by name keeps the module checkable on every platform, and the
#: runtime guard — not the annotation — remains what decides whether they exist.
_WinDLL: Any = getattr(ctypes, "WinDLL", None)
_get_last_error: Any = getattr(ctypes, "get_last_error", lambda: 0)


@runtime_checkable
class ProcessGuard(Protocol):
    """Attaches a spawned process to a lifetime-limited container."""

    def attach(self, pid: int) -> bool: ...
    def close(self) -> None: ...
    @property
    def description(self) -> str: ...


class NullProcessGuard:
    """No-op guard used on platforms without an implementation.

    It reports ``attach`` as failed rather than pretending the protection exists, so the
    manager can surface the reduced guarantee instead of assuming it.
    """

    def attach(self, pid: int) -> bool:
        return False

    def close(self) -> None:
        return None

    @property
    def description(self) -> str:
        return "none (orphan protection unavailable on this platform)"


if IS_WINDOWS:

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):  # noqa: N801 - Windows API name
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IO_COUNTERS(ctypes.Structure):  # noqa: N801 - Windows API name
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):  # noqa: N801 - Windows API name
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x100
    _JOB_OBJECT_LIMIT_JOB_MEMORY = 0x200
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_QUERY_INFORMATION = 0x0400

    class WindowsJobObjectGuard:
        """Job Object wrapper built on ``ctypes`` so no extra dependency is required."""

        def __init__(self, *, memory_limit_bytes: int | None = None) -> None:
            self._kernel32 = _WinDLL("kernel32", use_last_error=True)
            self._handle: int | None = None
            self._memory_limit_bytes = memory_limit_bytes
            self._closed = False

        @property
        def description(self) -> str:
            return "Windows Job Object (kill-on-close)"

        def _ensure_job(self) -> int | None:
            """Create the job on first use and return its handle."""
            if self._handle is not None:
                return self._handle
            handle = int(self._kernel32.CreateJobObjectW(None, None))
            if not handle:
                logger.error("CreateJobObject failed: %s", _get_last_error())
                return None
            info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if self._memory_limit_bytes:
                # Guest RAM is not a host cap: without this the QEMU process could grow
                # well past the configured guest memory.
                info.BasicLimitInformation.LimitFlags |= (
                    _JOB_OBJECT_LIMIT_PROCESS_MEMORY | _JOB_OBJECT_LIMIT_JOB_MEMORY
                )
                info.ProcessMemoryLimit = self._memory_limit_bytes
                info.JobMemoryLimit = self._memory_limit_bytes
            ok = self._kernel32.SetInformationJobObject(
                handle,
                _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            if not ok:
                logger.error("SetInformationJobObject failed: %s", _get_last_error())
                self._kernel32.CloseHandle(handle)
                return None
            self._handle = handle
            return handle

        def attach(self, pid: int) -> bool:
            if self._closed:
                return False
            handle = self._ensure_job()
            if handle is None:
                return False
            access = _PROCESS_SET_QUOTA | _PROCESS_TERMINATE | _PROCESS_QUERY_INFORMATION
            process = self._kernel32.OpenProcess(access, False, pid)
            if not process:
                logger.error("OpenProcess(%d) failed: %s", pid, _get_last_error())
                return False
            try:
                ok = self._kernel32.AssignProcessToJobObject(handle, process)
                if not ok:
                    logger.error("AssignProcessToJobObject failed: %s", _get_last_error())
                return bool(ok)
            finally:
                self._kernel32.CloseHandle(process)

        def close(self) -> None:
            self._closed = True
            if self._handle is not None:
                self._kernel32.CloseHandle(self._handle)
                self._handle = None


def create_process_guard(*, memory_limit_bytes: int | None = None) -> ProcessGuard:
    """Build the platform guard, falling back to the explicit no-op."""
    if IS_WINDOWS:
        return WindowsJobObjectGuard(memory_limit_bytes=memory_limit_bytes)
    return NullProcessGuard()
