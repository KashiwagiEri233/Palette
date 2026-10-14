"""Run against a hidden, real Windows console, never the test runner's terminal."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ctypes
import json
import msvcrt
import os
import sys
import threading
from ctypes import wintypes
from pathlib import Path

from palette.cli import main
from palette.stdio import StoppableInput
from palette.windows_console import WindowsConsoleInput


class KeyEvent(ctypes.Structure):
    _fields_ = [
        ("down", wintypes.BOOL),
        ("repeat", wintypes.WORD),
        ("key", wintypes.WORD),
        ("scan", wintypes.WORD),
        ("char", wintypes.WCHAR),
        ("control", wintypes.DWORD),
    ]


class Event(ctypes.Union):
    _fields_ = [("key", KeyEvent), ("padding", ctypes.c_byte * 16)]


class InputRecord(ctypes.Structure):
    _fields_ = [("kind", wintypes.WORD), ("event", Event)]


def console_api():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetFileType.argtypes = [wintypes.HANDLE]
    kernel.GetFileType.restype = wintypes.DWORD
    kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetConsoleMode.restype = wintypes.BOOL
    kernel.FlushConsoleInputBuffer.argtypes = [wintypes.HANDLE]
    kernel.FlushConsoleInputBuffer.restype = wintypes.BOOL
    kernel.WriteConsoleInputW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(InputRecord),
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel.WriteConsoleInputW.restype = wintypes.BOOL
    kernel.GenerateConsoleCtrlEvent.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.GenerateConsoleCtrlEvent.restype = wintypes.BOOL
    return kernel


def send_keys(api, handle: int, text: str) -> None:
    # INPUT_RECORD holds individual UTF-16 code units, not Python code points.
    encoded = text.encode("utf-16-le")
    records = (InputRecord * (len(encoded) // 2))()
    for index, offset in enumerate(range(0, len(encoded), 2)):
        char = chr(int.from_bytes(encoded[offset : offset + 2], "little"))
        records[index].kind = 1  # KEY_EVENT
        records[index].event.key = KeyEvent(True, 1, ord(char) if char in "\r\b" else 0, 0, char, 0)
    count = wintypes.DWORD()
    if not api.WriteConsoleInputW(handle, records, len(records), ctypes.byref(count)):
        raise ctypes.WinError(ctypes.get_last_error())
    assert count.value == len(records)


async def check_reader(fd: int, api, handle: int, mode: int) -> None:
    try:
        await WindowsConsoleInput(-1).read(asyncio.Event())
    except OSError as error:
        assert error.winerror == 6  # ERROR_INVALID_HANDLE is propagated after cleanup.
    else:
        raise AssertionError("invalid console read unexpectedly succeeded")
    assert not any(t.name == "palette-console-input" for t in threading.enumerate())
    for text, expected in [("你好🙂x\b!\r", "你好🙂!\r\n"), ("\x1a\r", "")]:
        api.FlushConsoleInputBuffer(handle)
        async with StoppableInput(fd, asyncio.Event()) as reader:
            assert reader._console is not None
            read = asyncio.create_task(reader.readline())
            await asyncio.sleep(0.1)
            assert not read.done()
            send_keys(api, handle, text)
            assert await asyncio.wait_for(read, 2) == expected
    for partial, cancel in [(False, False), (True, False), (True, True)]:
        api.FlushConsoleInputBuffer(handle)
        stopping = asyncio.Event()
        async with StoppableInput(fd, stopping) as reader:
            read = asyncio.create_task(reader.readline())
            await asyncio.sleep(0.1)
            if partial:
                send_keys(api, handle, "unfinished")
                await asyncio.sleep(0.1)
            assert not read.done()
            if cancel:
                read.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.wait_for(read, 2)
            else:
                stopping.set()
                assert await asyncio.wait_for(read, 2) == ""
            assert not any(t.name == "palette-console-input" for t in threading.enumerate())
    current_mode = wintypes.DWORD()
    assert api.GetConsoleMode(handle, ctypes.byref(current_mode))
    assert current_mode.value == mode


def run() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("case", choices=["reader", "serve"])
    args = parser.parse_args()
    # Popen creates a hidden console. Reopen its console input even when the
    # runner itself uses redirected stdio, proving GetConsoleMode really succeeds.
    fd = os.open("CONIN$", os.O_RDWR | os.O_BINARY)
    try:
        api = console_api()
        handle = msvcrt.get_osfhandle(fd)
        mode = wintypes.DWORD()
        assert api.GetFileType(handle) == 2
        assert api.GetConsoleMode(handle, ctypes.byref(mode))
        assert mode.value & 2  # ENABLE_LINE_INPUT: use the native cooked editor.
        args.root.joinpath("console.json").write_text(
            json.dumps({"file_type": 2, "mode": mode.value}), encoding="utf-8"
        )
        if args.case == "reader":
            asyncio.run(check_reader(fd, api, handle, mode.value))
            return 0
        os.dup2(fd, sys.stdin.fileno())
        from mcp.os.win32.utilities import rebind_std_handle_to_fd

        rebind_std_handle_to_fd(sys.stdin.fileno())
        finished = threading.Event()

        def feed_eof() -> None:
            while not finished.wait(0.025):
                if args.root.joinpath("console-eof").exists():
                    send_keys(api, handle, "\x1a\r")
                    return
                if args.root.joinpath("console-interrupt").exists():
                    if not api.GenerateConsoleCtrlEvent(0, 0):
                        raise ctypes.WinError(ctypes.get_last_error())
                    return

        feeder = threading.Thread(target=feed_eof, daemon=True)
        feeder.start()
        try:
            return main(["--root", str(args.root), "serve"])
        finally:
            finished.set()
            feeder.join(timeout=2)
    finally:
        os.close(fd)


if __name__ == "__main__":
    raise SystemExit(run())
