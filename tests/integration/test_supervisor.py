"""Real POSIX process ownership, independent of fake QMP behavior."""

import asyncio
import os
import signal
import sys

import psutil
import pytest

from palette.errors import ErrorCode, PaletteError
from palette.vm import launcher
from palette.vm.launcher import LaunchPlan, build_child_env, run_tool, spawn_qemu

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX supervisor")


def plan(tmp_path, code, **limits):
    return LaunchPlan(
        argv=[sys.executable, "-I", "-c", code],
        env=build_child_env(),
        cwd=tmp_path,
        accelerator="tcg",
        **limits,
    )


async def test_direct_stdio_and_liveness_cleanup(tmp_path):
    process = await spawn_qemu(
        plan(tmp_path, "import sys,time; print('ready', flush=True); time.sleep(60)")
    )
    assert await asyncio.wait_for(process.stdout.readline(), 3) == b"ready\n"
    assert psutil.pid_exists(process.pid)
    process.close()
    await asyncio.wait_for(process.wait(), 5)
    assert not psutil.pid_exists(process.pid)
    assert process.exit_reason == "owner-exited"


@pytest.mark.parametrize(
    "limits,reason",
    [
        ({"max_runtime_seconds": 0.15}, "runtime-limit"),
        ({"memory_limit_bytes": 1024}, "memory-limit"),
    ],
)
async def test_supervisor_enforces_budget(tmp_path, limits, reason):
    process = await spawn_qemu(plan(tmp_path, "import time; time.sleep(60)", **limits))
    await asyncio.wait_for(process.wait(), 5)
    assert not psutil.pid_exists(process.pid)
    assert process.exit_reason == reason


async def test_owner_sigkill_reaps_qemu(tmp_path):
    code = (
        "import asyncio,sys\nfrom pathlib import Path\n"
        "from palette.vm.launcher import LaunchPlan,build_child_env,spawn_qemu\n"
        "async def main():\n"
        " p=await spawn_qemu(LaunchPlan(argv=[sys.executable,'-I','-c','import time; time.sleep(60)'],"
        "env=build_child_env(),cwd=Path.cwd(),accelerator='tcg'))\n"
        " print(p.pid, flush=True)\n await asyncio.Event().wait()\n"
        "asyncio.run(main())\n"
    )
    owner = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-c",
        code,
        cwd=tmp_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    pid = None
    try:
        pid = int(await asyncio.wait_for(owner.stdout.readline(), 10))
        child = psutil.Process(pid)
        os.kill(owner.pid, signal.SIGKILL)
        await asyncio.wait_for(owner.communicate(), 5)
        await asyncio.to_thread(child.wait, timeout=5)
        assert not child.is_running()
    finally:
        if owner.returncode is None:
            owner.kill()
            await owner.communicate()
        if pid and psutil.pid_exists(pid):
            psutil.Process(pid).kill()


async def test_helper_cancellation_reaps_process(tmp_path):
    marker = tmp_path / "pid"
    code = "import os,time,pathlib,sys; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)"
    task = asyncio.create_task(run_tool([sys.executable, "-I", "-c", code, str(marker)]))
    async with asyncio.timeout(5):
        while not marker.exists():  # noqa: ASYNC110 - readiness crosses a process boundary
            await asyncio.sleep(0.01)
    pid = int(marker.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not psutil.pid_exists(pid)


def alive(pid):
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


async def test_helper_timeout_kills_pipe_holding_descendant_after_leader_exit(tmp_path):
    marker = tmp_path / "descendant"
    child_code = "import pathlib,os,sys,time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)"
    code = "import subprocess,sys; subprocess.Popen([sys.executable,'-I','-c',sys.argv[1],sys.argv[2]])"
    try:
        with pytest.raises(PaletteError) as error:
            await asyncio.wait_for(
                run_tool([sys.executable, "-I", "-c", code, child_code, str(marker)], timeout=0.3),
                timeout=5,
            )
        assert error.value.code == ErrorCode.TIMEOUT
        assert not alive(int(marker.read_text()))
    finally:
        if marker.exists() and alive(int(marker.read_text())):
            psutil.Process(int(marker.read_text())).kill()


async def test_supervisor_confirms_descendants_after_leader_exits(tmp_path):
    code = (
        "import subprocess,sys; "
        "p=subprocess.Popen([sys.executable,'-I','-c','import time; time.sleep(60)']); "
        "print(p.pid,flush=True)"
    )
    process = await spawn_qemu(plan(tmp_path, code))
    descendant = int(await asyncio.wait_for(process.stdout.readline(), 5))
    try:
        await asyncio.wait_for(process.wait(), 5)
        assert not alive(descendant)
    finally:
        process.kill()
        if alive(descendant):
            psutil.Process(descendant).kill()


async def test_wait_confirms_exit_with_unread_output_backpressure(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "STREAM_LIMIT", 1024)
    process = await spawn_qemu(
        plan(
            tmp_path,
            "import os,time; time.sleep(0.1)\nwhile True: os.write(1,b'x'*4096)",
            max_runtime_seconds=0.4,
        )
    )
    try:
        await asyncio.wait_for(process.wait(), 5)
        assert not alive(process.pid)
    finally:
        process.kill()
        await asyncio.wait_for(process.stdout.read(), 3)


async def test_cancellation_before_spawn_handoff_drains_and_reaps(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "STREAM_LIMIT", 1024)
    original = launcher._spawn_qemu
    entered = asyncio.Event()
    gate = asyncio.Event()
    spawned = []

    async def hold_handoff(launch_plan, **kwargs):
        process = await original(launch_plan, **kwargs)
        spawned.append(process)
        entered.set()
        await gate.wait()
        return process

    monkeypatch.setattr(launcher, "_spawn_qemu", hold_handoff)
    task = asyncio.create_task(
        spawn_qemu(plan(tmp_path, "import os\nwhile True: os.write(1,b'x'*4096)"))
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await asyncio.sleep(0.1)
        task.cancel()
        gate.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert not alive(spawned[0].pid)
    finally:
        gate.set()
        for process in spawned:
            process.kill()


async def test_failed_spawn_cleanup_preserves_registered_handle(tmp_path, monkeypatch):
    original_spawn = launcher._spawn_qemu
    original_dispose = launcher._dispose_spawned
    entered = asyncio.Event()
    gate = asyncio.Event()
    registered = []

    async def hold_handoff(launch_plan, **kwargs):
        process = await original_spawn(launch_plan, **kwargs)
        entered.set()
        await gate.wait()
        return process

    async def cannot_confirm(process):
        raise PaletteError(ErrorCode.TIMEOUT, "injected unconfirmed cleanup")

    monkeypatch.setattr(launcher, "_spawn_qemu", hold_handoff)
    monkeypatch.setattr(launcher, "_dispose_spawned", cannot_confirm)
    task = asyncio.create_task(
        spawn_qemu(plan(tmp_path, "import time; time.sleep(60)"), on_spawn=registered.append)
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert registered and alive(registered[0].pid)
        task.cancel()
        gate.set()
        with pytest.raises(PaletteError) as error:
            await asyncio.wait_for(task, 5)
        assert error.value.code == ErrorCode.TIMEOUT
        assert registered[0].returncode is None
    finally:
        gate.set()
        for process in registered:
            await original_dispose(process)


async def test_helper_creation_cancel_reports_unconfirmed_exit(tmp_path, monkeypatch):
    original_spawn = asyncio.create_subprocess_exec
    entered = asyncio.Event()
    gate = asyncio.Event()
    spawned = []

    async def hold_creation(*args, **kwargs):
        process = await original_spawn(*args, **kwargs)
        spawned.append(process)
        entered.set()
        await gate.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", hold_creation)
    monkeypatch.setattr(launcher, "terminate_process_group", lambda _pid: False)
    task = asyncio.create_task(
        run_tool([sys.executable, "-I", "-c", "import time; time.sleep(60)"])
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        gate.set()
        with pytest.raises(PaletteError) as error:
            await asyncio.wait_for(task, 2)
        assert error.value.code == ErrorCode.TIMEOUT
        assert alive(spawned[0].pid)
    finally:
        gate.set()
        for process in spawned:
            if process.returncode is None:
                process.kill()
            await process.communicate()
