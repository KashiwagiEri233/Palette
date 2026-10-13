"""POSIX QEMU parent. Its standard streams belong exclusively to the child."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import select
import time

import psutil

from .launcher import spawn_supervised_child
from .managed import terminate_process_group


def _report(fd: int, **fields: object) -> None:
    with contextlib.suppress(OSError):
        os.write(fd, json.dumps(fields).encode("utf-8") + b"\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--liveness-fd", type=int, required=True)
    parser.add_argument("--status-fd", type=int, required=True)
    parser.add_argument("--memory-limit", type=int, default=0)
    parser.add_argument("--runtime-limit", type=float, default=0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    child = None
    created = None
    reason = "exited"
    try:
        # Do not create a guest if its owner died while the supervisor was starting.
        if select.select([args.liveness_fd], [], [], 0)[0]:
            return 1
        child = spawn_supervised_child(command)
        tracked = psutil.Process(child.pid)
        created = tracked.create_time()
        _report(args.status_fd, pid=child.pid, created=created)
        for fd in (0, 1, 2):
            with contextlib.suppress(OSError):
                os.close(fd)
        started = time.monotonic()
        while child.poll() is None:
            if select.select([args.liveness_fd], [], [], 0.25)[0]:
                request = os.read(args.liveness_fd, 1024)
                reason = "owner-exited" if not request else "stopped"
                break
            if args.runtime_limit and time.monotonic() - started >= args.runtime_limit:
                reason = "runtime-limit"
                break
            if args.memory_limit:
                rss = 0
                with contextlib.suppress(psutil.Error):
                    for process in [tracked, *tracked.children(recursive=True)]:
                        with contextlib.suppress(psutil.Error):
                            rss += process.memory_info().rss
                if rss > args.memory_limit:
                    reason = "memory-limit"
                    break
    except Exception as exc:
        _report(args.status_fd, error=f"QEMU supervisor failed: {type(exc).__name__}")
    finally:
        if child is not None:
            # Also remove descendants if the QEMU leader exited by itself.
            if terminate_process_group(child.pid, created):
                code = child.wait()
                _report(args.status_fd, returncode=code, reason=reason)
            else:
                _report(args.status_fd, error="QEMU process group exit could not be confirmed")
        for fd in (args.liveness_fd, args.status_fd):
            with contextlib.suppress(OSError):
                os.close(fd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
