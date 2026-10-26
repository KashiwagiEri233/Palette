"""Compile the real inbox-runtime Windows worker and exercise its framed stdio path.

These tests touch only temporary test files and a disposable HKCU test key. They never
install the service, open a VirtIO port, change the clipboard or send desktop input.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from palette.guest_tools.protocol import FrameDecoder, encode_frame

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="Windows inbox .NET compiler required"
)
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def worker_exe(tmp_path_factory):
    root = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET/Framework64/v4.0.30319"
    compiler = root / "csc.exe"
    if not compiler.exists():
        pytest.skip("inbox .NET Framework compiler unavailable")
    executable = tmp_path_factory.mktemp("windows-worker") / "Palette.GuestTools.exe"
    references = ["System.Web.Extensions.dll", "System.ServiceProcess.dll"] + [
        str(root / "WPF" / name)
        for name in ("UIAutomationClient.dll", "UIAutomationTypes.dll", "WindowsBase.dll")
    ]
    compiled = subprocess.run(  # noqa: S603 - fixed inbox compiler and repository sources
        [
            str(compiler),
            "/nologo",
            "/target:exe",
            "/platform:x64",
            "/out:" + str(executable),
            *["/reference:" + value for value in references],
            *[
                str(ROOT / "guest/windows" / name)
                for name in ("Palette.Agent.cs", "Palette.Native.cs", "Palette.Operations.cs")
            ],
        ],
        capture_output=True,
        timeout=30,
    )
    assert compiled.returncode == 0, compiled.stdout.decode(
        errors="replace"
    ) + compiled.stderr.decode(errors="replace")
    return executable


def requests(executable, operations):
    hello = {
        "v": "1.1",
        "kind": "hello",
        "id": "connection",
        "payload": {
            "versions": ["1.1"],
            "connect_id": "connection",
            "boot_epoch": 1,
            "desired_capabilities": ["guest." + operation for operation, _ in operations],
        },
    }
    data = encode_frame(hello)
    for index, (operation, parameters) in enumerate(operations):
        data += encode_frame(
            {
                "v": "1.1",
                "kind": "request",
                "id": "r" + str(index),
                "payload": {
                    "connect_id": "connection",
                    "boot_epoch": 1,
                    "generation": 1,
                    "capability": "guest." + operation,
                    "params": parameters,
                    "timeout_ms": 10000,
                },
            }
        )
    completed = subprocess.run(  # noqa: S603 - executable compiled by this fixture
        [str(executable), "--stdio"], input=data, capture_output=True, timeout=30
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    messages = FrameDecoder().feed(completed.stdout)
    assert len(messages) == len(operations) + 1
    assert messages[0]["kind"] == "hello_ack"
    return [message["payload"] for message in messages[1:]]


def test_real_worker_unicode_file_roundtrip_copy_move_search_and_delete(worker_exe, tmp_path):
    source = str(tmp_path / "中文 😀.txt")
    copy = str(tmp_path / "copy.txt")
    moved = str(tmp_path / "moved.txt")
    results = requests(
        worker_exe,
        [
            ("filesystem", {"mode": "write", "path": source, "content": "中文 😀\nsecond\n"}),
            ("filesystem", {"mode": "read", "path": source, "offset": 2, "limit": 1}),
            ("filesystem", {"mode": "copy", "path": source, "destination": copy}),
            ("filesystem", {"mode": "move", "path": copy, "destination": moved}),
            ("filesystem", {"mode": "search", "path": str(tmp_path), "pattern": "*.txt"}),
            ("filesystem", {"mode": "delete", "path": moved}),
        ],
    )
    assert all(result["ok"] for result in results), results
    assert results[1]["data"]["content"] == "second\n"
    assert len(results[4]["data"]["items"]) == 2
    assert not Path(copy).exists() and not Path(moved).exists()
    assert Path(source).read_text(encoding="utf-8").startswith("中文 😀")


def test_real_worker_shell_output_exit_code_timeout_and_truncation(worker_exe):
    results = requests(
        worker_exe,
        [
            ("powershell", {"command": "[Console]::Write('中文 😀'); exit 7"}),
            ("powershell", {"command": "Start-Sleep -Seconds 10", "timeout_seconds": 1}),
            ("powershell", {"command": "[Console]::Write(('a' * 40000))"}),
        ],
    )
    assert all(result["ok"] for result in results), results
    assert results[0]["data"]["stdout"] == "中文 😀"
    assert results[0]["data"]["exit_code"] == 7
    assert results[1]["data"]["timed_out"]
    assert results[2]["data"]["truncated"]
    assert len(results[2]["data"]["stdout"]) == 16384


def test_real_worker_registry_preserves_types_and_cleans_up(worker_exe):
    import winreg

    key = "Software\\PaletteTest-" + uuid.uuid4().hex
    path = "HKCU:\\" + key
    try:
        results = requests(
            worker_exe,
            [
                (
                    "registry",
                    {
                        "mode": "set",
                        "path": path,
                        "name": "bytes",
                        "type": "Binary",
                        "value": [0, 128, 255],
                    },
                ),
                ("registry", {"mode": "get", "path": path, "name": "bytes"}),
                (
                    "registry",
                    {
                        "mode": "set",
                        "path": path,
                        "name": "multi",
                        "type": "MultiString",
                        "value": ["中文", "😀"],
                    },
                ),
                ("registry", {"mode": "get", "path": path, "name": "multi"}),
                (
                    "registry",
                    {
                        "mode": "set",
                        "path": path,
                        "name": "large",
                        "type": "QWord",
                        "value": "18446744073709551615",
                    },
                ),
                ("registry", {"mode": "get", "path": path, "name": "large"}),
                ("registry", {"mode": "delete", "path": path}),
            ],
        )
        assert all(result["ok"] for result in results), results
        # JavaScriptSerializer represents byte[] as base64, which must not leak as a
        # different public type than the byte array accepted by the tool.
        assert results[1]["data"]["value"] == [0, 128, 255]
        assert results[3]["data"]["value"] == ["中文", "😀"]
        assert results[5]["data"]["value"] == "18446744073709551615"
    finally:
        with contextlib.suppress(FileNotFoundError):
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)


def test_real_worker_does_not_overwrite_destination_for_invalid_source(worker_exe, tmp_path):
    destination = tmp_path / "preserved.txt"
    destination.write_text("keep", encoding="utf-8")
    result = requests(
        worker_exe,
        [
            (
                "filesystem",
                {
                    "mode": "copy",
                    "path": str(tmp_path / "missing"),
                    "destination": str(destination),
                    "overwrite": True,
                },
            )
        ],
    )[0]
    assert not result["ok"]
    assert destination.read_text() == "keep"
