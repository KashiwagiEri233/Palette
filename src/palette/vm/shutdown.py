"""Bounded guest power requests and a separate, explicit emulator stop path."""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from ..errors import ErrorCode, PaletteError

if TYPE_CHECKING:
    from .manager import VmInstance


def shutdown_timeout(instance: VmInstance) -> PaletteError:
    return PaletteError(
        ErrorCode.TIMEOUT,
        "the guest did not shut down within the configured budget",
        details={
            "operation": "guest-shutdown",
            "vm_id": instance.view.vm_id,
            "instance_id": instance.view.instance_id,
            "timeout_seconds": instance.profile.timeouts.shutdown_seconds,
        },
    )


async def _request(instance: VmInstance, resume: bool) -> None:
    if instance.process is None or instance.process.returncode is not None:
        return
    qmp = instance.qmp
    if resume:
        if qmp is None or not qmp.connected:
            raise PaletteError(ErrorCode.QMP_UNAVAILABLE, "graceful shutdown is unavailable")
        await qmp.execute("cont")
        instance.shutdown_resumed = True
    agent = instance.qemu_guest_agent
    sent = False
    if agent is not None and agent.online and "guest-shutdown" in agent.commands:
        try:
            await agent.shutdown()
            sent = True
        except PaletteError as exc:
            # An uncertain write must never be replayed through another channel.
            if exc.code not in {ErrorCode.GUEST_TOOL_UNAVAILABLE, ErrorCode.UNSUPPORTED_CAPABILITY}:
                raise
    if not sent:
        if qmp is None or not qmp.connected:
            raise PaletteError(ErrorCode.QMP_UNAVAILABLE, "graceful shutdown is unavailable")
        await qmp.execute("system_powerdown")
    if instance.process is not None:
        await instance.process.wait()


async def wait_for_guest(instance: VmInstance, *, resume: bool = False) -> bool:
    """Return whether an authorized force request interrupted the guest wait."""
    if instance.force_shutdown.is_set():
        return True
    request = asyncio.create_task(_request(instance, resume), name="guest-shutdown-request")
    force = asyncio.create_task(instance.force_shutdown.wait(), name="guest-shutdown-escalation")
    try:
        async with asyncio.timeout(instance.profile.timeouts.shutdown_seconds):
            await asyncio.wait({request, force}, return_when=asyncio.FIRST_COMPLETED)
            if instance.force_shutdown.is_set():
                return True
            await request
            return False
    except TimeoutError:
        raise shutdown_timeout(instance) from None
    except PaletteError as exc:
        raise PaletteError(
            exc.code,
            exc.message,
            details={
                **exc.details,
                "operation": "guest-shutdown",
                "vm_id": instance.view.vm_id,
                "instance_id": instance.view.instance_id,
            },
            retryable=exc.retryable,
        ) from exc
    finally:
        request.cancel()
        force.cancel()
        await asyncio.gather(request, force, return_exceptions=True)


async def force_exit(instance: VmInstance) -> None:
    """Ask QEMU itself to quit, then kill the owned process/job if it cannot exit."""
    process = instance.process
    if process is None or process.returncode is not None:
        return
    budget = instance.profile.timeouts.force_kill_seconds
    if instance.qmp is not None and instance.qmp.connected:
        with contextlib.suppress(PaletteError, TimeoutError, OSError):
            async with asyncio.timeout(min(1.0, budget)):
                await instance.qmp.execute("quit", timeout=min(1.0, budget))
        try:
            await asyncio.wait_for(process.wait(), timeout=min(1.0, budget))
            return
        except TimeoutError:
            pass
    with contextlib.suppress(OSError):
        process.kill()
    # Closing the owned guard also removes descendants and works if QMP is broken.
    if instance.guard is not None:
        instance.guard.close()
    try:
        await asyncio.wait_for(process.wait(), timeout=budget)
    except TimeoutError:
        raise PaletteError(
            ErrorCode.TIMEOUT,
            "QEMU exit could not be confirmed after killing it",
            details={
                "operation": "force-stop",
                "vm_id": instance.view.vm_id,
                "instance_id": instance.view.instance_id,
            },
        ) from None
