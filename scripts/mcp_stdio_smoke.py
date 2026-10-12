"""Drive `palette serve` over real stdio, the way an MCP client would.

This is a smoke test of the last untested seam: the process boundary, the stdio transport
and the JSON-RPC session. Everything above it is covered in-process; everything below is
covered by the fake QEMU. Run it by hand against a configured profile:

    uv run python scripts/mcp_stdio_smoke.py

It prints the server's identity, the tool list, and the result of a ``vm_status`` call,
then sends a malformed JSON-RPC frame to confirm the session survives it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = "2025-11-25"


def build_frame(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload) + "\n").encode("utf-8")


class Session:
    def __init__(self, command: list[str], cwd: Path) -> None:
        # A deliberately thin environment, so the session is exercised the way a client would
        # start it rather than inheriting this shell's state. HOME/USERPROFILE and LANG are in
        # the list for portability: the interpreter wants the first two on POSIX and the third
        # decides how it encodes its own output on either platform.
        keep = {
            "SYSTEMROOT",
            "WINDIR",
            "SYSTEMDRIVE",
            "PATH",
            "TEMP",
            "TMP",
            "HOME",
            "USERPROFILE",
            "LANG",
            "LC_ALL",
        }
        env = {
            **{key: value for key, value in os.environ.items() if key in keep},
            "PYTHONIOENCODING": "utf-8",
        }
        self.process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            command,
            cwd=str(cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        self.counter = 0

    def call(
        self, method: str, params: dict[str, Any] | None = None, *, notify: bool = False
    ) -> dict[str, Any] | None:
        assert self.process.stdin is not None
        self.counter += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if not notify:
            payload["id"] = self.counter
        if params is not None:
            payload["params"] = params
        self.process.stdin.write(build_frame(payload))
        self.process.stdin.flush()
        if notify:
            return None
        assert self.process.stdout is not None
        while True:
            line = self.process.stdout.readline()
            if not line:
                raise RuntimeError("the server closed stdout")
            message: Any = json.loads(line.decode("utf-8"))
            if isinstance(message, dict) and message.get("id") == self.counter:
                return message

    def raw(self, data: bytes) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(data)
        self.process.stdin.flush()

    def close(self) -> str:
        if self.process.stdin is not None:
            self.process.stdin.close()
        stderr = b""
        if self.process.stderr is not None:
            stderr = self.process.stderr.read()
        self.process.wait(timeout=20)
        return stderr.decode("utf-8", "replace")


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    session = Session([sys.executable, "-m", "palette.cli", "serve"], root)
    try:
        initialize = session.call(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "stdio-smoke", "version": "1"},
            },
        )
        assert initialize is not None
        result = initialize.get("result") or initialize.get("error")
        print("initialize:", json.dumps(result, indent=2)[:400])
        session.call("notifications/initialized", notify=True)

        tools = session.call("tools/list")
        assert tools is not None
        names = sorted(tool["name"] for tool in tools["result"]["tools"])
        print("tools:", names)

        schemas = {
            tool["name"]: "yes" if tool.get("outputSchema") else "no"
            for tool in tools["result"]["tools"]
        }
        print("outputSchema:", schemas)

        status = session.call(
            "tools/call", {"name": "vm_status", "arguments": {"vm_id": "desktop"}}
        )
        assert status is not None
        body = status["result"]["structuredContent"]
        print("vm_status ok:", body["ok"], "code:", (body.get("error") or {}).get("code"))

        observe = session.call(
            "tools/call", {"name": "vm_observe", "arguments": {"vm_id": "desktop"}}
        )
        assert observe is not None
        print("vm_observe isError:", observe["result"].get("isError"))
        print("  content types:", [block["type"] for block in observe["result"]["content"]])

        # A malformed frame must not take the session down.
        session.raw(b"{ this is not json }\n")
        after = session.call("tools/list")
        assert after is not None
        print("session survived a malformed frame:", len(after["result"]["tools"]), "tools")
    finally:
        stderr = session.close()
    if stderr.strip():
        print("--- server stderr ---")
        print(stderr.strip()[-1500:])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
