"""Configuration budgets, JSON limits, and native resource failure paths."""

from __future__ import annotations

import asyncio
import os

import pytest

from palette.errors import ErrorCode
from palette.operator import OperatorServer
from palette.operator import paths as operator_paths
from palette.operator.paths import RuntimeLease, RuntimePaths
from palette.operator.protocol import MAX_CLIENTS, Request, decode_json
from palette.operator.server import operation_budget


@pytest.mark.parametrize(
    "payload",
    [b'{"x":NaN}', b'{"x":Infinity}', b'{"x":1,"x":2}', b'{"x":{"a":1,"a":2}}'],
)
def test_nonfinite_numbers_and_duplicate_keys_are_rejected(payload) -> None:
    with pytest.raises(ValueError):
        decode_json(payload)


def test_operation_budget_includes_trusted_slow_boot_and_cleanup(config) -> None:
    profile = config.profile("default")
    slower = profile.model_copy(
        update={"timeouts": profile.timeouts.model_copy(update={"startup_seconds": 600.0})}
    )
    configured = config.model_copy(update={"profiles": {"default": slower}})
    budget = operation_budget(configured)
    assert budget > 600 * MAX_CLIENTS
    assert budget > operation_budget(config)


def test_a_new_lease_fd_is_closed_if_private_permissions_fail(runtime_root, monkeypatch) -> None:
    lease = RuntimeLease(RuntimePaths(runtime_root))
    opened = []
    original_open = os.open

    def capture_open(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def refuse(_path):
        raise PermissionError("simulated ACL failure")

    monkeypatch.setattr(operator_paths.os, "open", capture_open)
    monkeypatch.setattr(operator_paths, "_secure_new_file", refuse)
    with pytest.raises(PermissionError):
        lease.acquire()
    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])


async def test_pending_operations_are_bounded_independently_of_connections(
    service, monkeypatch
) -> None:
    owner = OperatorServer(service)
    release = asyncio.Event()

    async def slow_route(_command):
        await release.wait()
        return {"ok": True}

    monkeypatch.setattr(owner, "_route", slow_route)

    def request(index):
        return Request.model_validate(
            {
                "generation": owner.endpoint.generation,
                "token": owner.endpoint.token,
                "request_id": f"{index:032x}",
                "command": {"method": "status", "vm_id": "desktop"},
            }
        )

    tasks = [
        asyncio.create_task(owner._execute_once(request(index))) for index in range(MAX_CLIENTS)
    ]
    try:
        await asyncio.sleep(0)
        refused = await owner._execute_once(request(MAX_CLIENTS))
        assert not refused.ok
        assert refused.error.code == ErrorCode.BUSY
        assert len(owner._requests) == MAX_CLIENTS
    finally:
        release.set()
        await asyncio.gather(*tasks)
