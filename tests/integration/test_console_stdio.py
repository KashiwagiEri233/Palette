"""Real Windows console stdin, native editing, cancellation and owner lifecycle."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_operator_ipc import cli, process_alive, write_config

from palette.cli import EXIT_RUNTIME
from palette.operator import OperatorClient
from palette.operator.paths import RuntimeLease, RuntimePaths
from support.profiles import make_profile

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="requires a Windows console")
HARNESS = Path(__file__).parents[1] / "support" / "console_stdio.py"


def start_console(root: Path, case: str, output):
    startup = subprocess.STARTUPINFO()
    startup.dwFlags = subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0  # SW_HIDE: no interactive window on the user's desktop.
    return subprocess.Popen(  # noqa: S603 - fixed test harness and explicit argument vector
        [sys.executable, str(HARNESS), str(root), case],
        startupinfo=startup,
        creationflags=subprocess.CREATE_NEW_CONSOLE,
        stdout=output,
        stderr=output,
    )


async def test_console_unicode_editing_eof_and_cancelled_reads(tmp_path: Path) -> None:
    with tmp_path.joinpath("output.log").open("w+b") as output:
        process = start_console(tmp_path, "reader", output)
        try:
            code = await asyncio.to_thread(process.wait, timeout=15)
            output.seek(0)
            assert code == 0, output.read().decode(errors="replace")
            assert json.loads(tmp_path.joinpath("console.json").read_text())["file_type"] == 2
        finally:
            if process.poll() is None:
                process.kill()
                await asyncio.to_thread(process.wait, timeout=5)


@pytest.mark.parametrize("exit_method", ["stdin", "runtime.stop", "interrupt"])
async def test_console_serve_survives_idle_stdin_and_cli_until_exit(
    tmp_path: Path, base_image: Path, exit_method: str
) -> None:
    profile = make_profile(base_image)
    wrapper = Path(__file__).parents[1] / "support" / "operator_qemu.py"
    profile = profile.model_copy(
        update={"qemu": profile.qemu.model_copy(update={"argv_prefix": ["-I", str(wrapper)]})}
    )
    runtime = tmp_path / "runtime"
    write_config(tmp_path, profile, runtime)
    with tmp_path.joinpath("output.log").open("w+b") as output:
        process = start_console(tmp_path, "serve", output)
        try:
            async with asyncio.timeout(10):
                while True:
                    assert process.poll() is None
                    try:
                        RuntimePaths(runtime).read_endpoint()
                        break
                    except (OSError, ValueError):
                        await asyncio.sleep(0.025)
            await asyncio.sleep(0.2)
            assert process.poll() is None  # Console stdin is idle; serve must stay alive.
            code, started, error = await cli(tmp_path, "start", "--profile", "default")
            assert code == 0, error.decode()
            code, status, error = await cli(tmp_path, "status")
            assert code == 0, error.decode()
            assert json.loads(status)["status"]["instance_id"] == json.loads(started)["instance_id"]
            pid_file = next((runtime / "vms").rglob("*.pid"))
            qemu_pid = int(pid_file.read_text())
            assert process_alive(qemu_pid)
            client = OperatorClient(runtime)
            if exit_method == "stdin":
                tmp_path.joinpath("console-eof").touch()
            elif exit_method == "interrupt":
                tmp_path.joinpath("console-interrupt").touch()
            else:
                await client.call("vms.stop", vm_id="desktop")
                assert await client.call("runtime.stop") == {"stopping": True}
            code = await asyncio.to_thread(process.wait, timeout=15)
            output.seek(0)
            assert code == (EXIT_RUNTIME if exit_method == "interrupt" else 0), (
                output.read().decode(errors="replace")
            )
            assert not process_alive(qemu_pid)
            assert not RuntimePaths(runtime).metadata.exists()
            lease = RuntimeLease(RuntimePaths(runtime))
            lease.acquire()
            lease.release()
        finally:
            if process.poll() is None:
                process.kill()
                await asyncio.to_thread(process.wait, timeout=5)
