"""Exercise idle, fragmented and closed stdio pipes without an MCP client."""

from __future__ import annotations

import asyncio
import os

from palette.stdio import StoppableInput


async def test_idle_pipe_stops_with_writer_still_open() -> None:
    read_fd, write_fd = os.pipe()
    stopping = asyncio.Event()
    try:
        async with StoppableInput(read_fd, stopping) as reader:
            read = asyncio.create_task(reader.readline())
            await asyncio.sleep(0.05)
            assert not read.done()
            stopping.set()
            assert await asyncio.wait_for(read, 1) == ""
            os.write(write_fd, b"client remains open\n")
    finally:
        os.close(read_fd)
        os.close(write_fd)


async def test_fragmented_utf8_lines_and_eof() -> None:
    read_fd, write_fd = os.pipe()
    try:
        async with StoppableInput(read_fd, asyncio.Event()) as reader:
            read = asyncio.create_task(reader.readline())
            encoded = "请退出\r\nsecond\nlast".encode()
            os.write(write_fd, encoded[:2])
            await asyncio.sleep(0.05)
            assert not read.done()
            os.write(write_fd, encoded[2:])
            assert await asyncio.wait_for(read, 1) == "请退出\r\n"
            assert await reader.readline() == "second\n"
            os.close(write_fd)
            write_fd = -1
            assert await asyncio.wait_for(reader.readline(), 1) == "last"
            assert await reader.readline() == ""
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)
