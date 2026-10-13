"""Real local transports and CLI processes connected to a persistent VM owner."""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import json
import os
import stat
import struct
import sys
from pathlib import Path

import pytest
from anyio import CancelScope

from palette.config import PaletteConfig, Profile
from palette.errors import ErrorCode, PaletteError
from palette.operator import OperatorClient, OperatorServer
from palette.operator.paths import IS_WINDOWS, RuntimePaths, _check_private
from palette.operator.protocol import Request, Response, receive_frame, send_frame
from palette.operator.unix import UnixChannel
from palette.server import build_server
from palette.service import PaletteService
from support.profiles import make_profile


def write_config(root: Path, profile: Profile, runtime: Path) -> None:
    def tables(data, prefix=""):
        lines = [f"[{prefix}]"] if prefix else []
        for key, value in data.items():
            if value is not None and not isinstance(value, dict):
                lines.append(f"{key} = {json.dumps(value)}")
        for key, value in data.items():
            if isinstance(value, dict):
                lines.extend(tables(value, f"{prefix}.{key}" if prefix else key))
        return lines

    (root / "profiles").mkdir(exist_ok=True)
    (root / "profiles" / "default.toml").write_text(
        "\n".join(tables(profile.model_dump(mode="json"))),
        encoding="utf-8",
    )
    (root / "palette.toml").write_text(
        "\n".join(
            tables(
                {
                    "host": {
                        "runtime_root": str(runtime),
                        "default_profile": "default",
                        "audit_enabled": False,
                    }
                }
            )
        ),
        encoding="utf-8",
    )


async def cli(root: Path, *arguments: str):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "palette.cli",
        "--root",
        str(root),
        "vm",
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
    except BaseException:
        process.kill()
        await process.wait()
        raise
    return process.returncode, stdout, stderr


async def raw_channel(endpoint):
    if IS_WINDOWS:
        from palette.operator.windows import connect

        return await connect(endpoint.address)
    return UnixChannel(*(await asyncio.open_unix_connection(endpoint.address)))


def wire_request(owner: OperatorServer, **command) -> Request:
    return Request.model_validate(
        {
            "generation": owner.endpoint.generation,
            "token": owner.endpoint.token,
            "request_id": "c" * 32,
            "command": command,
        }
    )


async def test_cli_start_status_and_stop_share_the_owner(
    service: PaletteService, tmp_path: Path
) -> None:
    write_config(tmp_path, service.config.profile("default"), service.config.runtime_root)
    async with OperatorServer(service):
        code, output, error = await cli(tmp_path, "start", "--profile", "default")
        assert code == 0, error.decode()
        started = json.loads(output)
        instance = service.manager.get("desktop")
        assert instance.process is not None and instance.process.returncode is None
        code, output, error = await cli(tmp_path, "status")
        assert code == 0, error.decode()
        assert json.loads(output)["status"]["instance_id"] == started["instance_id"]
        code, output, error = await cli(tmp_path, "take-control", "--reason", "operator test")
        assert code == 0, error.decode()
        assert json.loads(output)["controller_epoch"] == instance.view.controller_epoch
        assert (await service.observe("desktop")).ok
        code, output, error = await cli(tmp_path, "stop", "--force")
        assert code == 0, error.decode()
        assert instance.process.returncode is not None


async def test_cli_without_an_owner_explains_how_to_connect(
    config: PaletteConfig, tmp_path: Path
) -> None:
    write_config(tmp_path, config.profile("default"), config.runtime_root)
    code, _, error = await cli(tmp_path, "status")
    assert code != 0
    assert b"palette serve" in error
    assert b"TypeError" not in error


async def test_runtime_lease_and_metadata_are_private(service: PaletteService) -> None:
    first = OperatorServer(service)
    await first.start()
    try:
        _check_private(first.paths.directory, directory=True)
        _check_private(first.paths.metadata)
        if not IS_WINDOWS:
            assert stat.S_IMODE(Path(first.endpoint.address).stat().st_mode) == 0o600
        second = OperatorServer(service)
        with pytest.raises(PaletteError) as info:
            await second.start()
        assert info.value.code == ErrorCode.STATE_CONFLICT
        # The failed second start must not remove the first owner's discovery record.
        result = await OperatorClient(service.config.runtime_root).call("status", vm_id="missing")
        assert result["ok"] is False
    finally:
        await first.stop()
        first.release()


async def test_a_second_serve_process_cannot_take_over(
    service: PaletteService, tmp_path: Path
) -> None:
    write_config(tmp_path, service.config.profile("default"), service.config.runtime_root)
    async with OperatorServer(service):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "palette.cli",
            "--root",
            str(tmp_path),
            "serve",
            "--standalone",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(process.communicate(), 10)
        assert process.returncode == 4
        assert b"STATE_CONFLICT" in stderr
        assert b"Traceback" not in stderr
        assert (await OperatorClient(service.config.runtime_root).call("status", vm_id="missing"))[
            "ok"
        ] is False


@pytest.mark.parametrize("change", ["token", "generation", "method", "oversize"])
async def test_invalid_peers_are_rejected_without_disabling_the_owner(
    service: PaletteService, change: str
) -> None:
    async with OperatorServer(service) as owner:
        channel = await raw_channel(owner.endpoint)
        payload = wire_request(owner, method="status", vm_id="desktop").model_dump()
        if change in {"token", "generation"}:
            payload[change] = "0" * len(payload[change])
        if change == "method":
            payload["command"] = {"method": "execute", "command": "quit"}
        body = json.dumps(payload).encode()
        try:
            await channel.write(
                struct.pack(">I", 999999 if change == "oversize" else len(body)) + body
            )
            with pytest.raises((OSError, EOFError, asyncio.IncompleteReadError)):
                await asyncio.wait_for(channel.readexactly(1), 2)
        finally:
            await channel.close()
        assert (await OperatorClient(service.config.runtime_root).call("status", vm_id="missing"))[
            "ok"
        ] is False


async def test_concurrent_identical_operator_requests_execute_once(service: PaletteService) -> None:
    async with OperatorServer(service) as owner:
        channels = await asyncio.gather(raw_channel(owner.endpoint), raw_channel(owner.endpoint))
        request = wire_request(owner, method="start", vm_id="desktop", profile_id="default")
        try:
            await asyncio.gather(*(send_frame(channel, request) for channel in channels))
            responses = [
                Response.model_validate_json(body)
                for body in await asyncio.gather(
                    *(receive_frame(channel, timeout=20) for channel in channels)
                )
            ]
            assert all(response.ok for response in responses)
            assert responses[0].result["instance_id"] == responses[1].result["instance_id"]
        finally:
            await asyncio.gather(*(channel.close() for channel in channels))


async def test_closing_owner_cancels_partial_clients_and_allows_restart(
    service: PaletteService,
) -> None:
    owner = OperatorServer(service)
    await owner.start()
    channel = await raw_channel(owner.endpoint)
    await channel.write(b"\0")
    try:
        await asyncio.wait_for(owner.stop(), 2)
    finally:
        owner.release()
        await channel.close()
    async with OperatorServer(service) as replacement:
        assert replacement.endpoint.generation != owner.endpoint.generation
        assert not replacement._handlers


async def test_mcp_cancel_scope_does_not_skip_vm_or_endpoint_cleanup(
    service: PaletteService,
) -> None:
    server = build_server(service)
    process = None
    with CancelScope() as scope:
        async with server.settings.lifespan(server):
            await OperatorClient(service.config.runtime_root).call(
                "start", vm_id="desktop", profile_id="default"
            )
            process = service.manager.get("desktop").process
            scope.cancel()
            await asyncio.sleep(0)
    assert process is not None and process.returncode is not None
    assert not RuntimePaths(service.config.runtime_root).metadata.exists()


@pytest.mark.skipif(IS_WINDOWS, reason="Unix socket artifact handling")
async def test_stale_socket_is_replaced_but_a_regular_file_is_preserved(
    service: PaletteService,
) -> None:
    import socket

    paths = RuntimePaths(service.config.runtime_root)
    paths.prepare()
    socket_path = Path(paths.address("a" * 32))
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(socket_path))
    os.chmod(socket_path, 0o600)
    stale.close()
    async with OperatorServer(service):
        assert (await OperatorClient(service.config.runtime_root).call("status", vm_id="missing"))[
            "ok"
        ] is False
    socket_path.write_text("keep me", encoding="ascii")
    socket_path.chmod(0o600)
    try:
        with pytest.raises(PermissionError):
            await OperatorServer(service).start()
        assert socket_path.read_text(encoding="ascii") == "keep me"
    finally:
        socket_path.unlink()


def process_alive(pid: int) -> bool:
    if IS_WINDOWS:
        kernel = getattr(ctypes, "WinDLL", None)("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.WaitForSingleObject.restype = ctypes.c_uint32
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.restype = ctypes.c_int
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 258
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def test_real_serve_owns_cli_vms_until_stdio_closes(base_image: Path, tmp_path: Path) -> None:
    profile = make_profile(base_image)
    wrapper = Path(__file__).parents[1] / "support" / "operator_qemu.py"
    profile = profile.model_copy(
        update={"qemu": profile.qemu.model_copy(update={"argv_prefix": ["-I", str(wrapper)]})}
    )
    runtime = tmp_path / "runtime"
    write_config(tmp_path, profile, runtime)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "palette.cli",
        "--root",
        str(tmp_path),
        "serve",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stderr = asyncio.create_task(process.stderr.read())
    try:
        async with asyncio.timeout(10):
            while True:
                try:
                    RuntimePaths(runtime).read_endpoint()
                    break
                except (OSError, ValueError):
                    await asyncio.sleep(0.025)
        code, output, error = await cli(tmp_path, "start", "--profile", "default")
        assert code == 0, error.decode()
        started = json.loads(output)
        pid_files = list((runtime / "vms").rglob("*.pid"))
        assert len(pid_files) == 1
        pid = int(pid_files[0].read_text(encoding="ascii"))
        assert process_alive(pid)
        code, output, error = await cli(tmp_path, "status")
        assert code == 0, error.decode()
        assert json.loads(output)["status"]["instance_id"] == started["instance_id"]
        process.stdin.close()
        await asyncio.wait_for(process.wait(), 20)
        assert process.returncode == 0, (await stderr).decode()
        async with asyncio.timeout(5):
            while process_alive(pid):  # noqa: ASYNC110 - another OS process has no asyncio event
                await asyncio.sleep(0.025)
        assert not RuntimePaths(runtime).metadata.exists()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        with contextlib.suppress(asyncio.CancelledError):
            await stderr
