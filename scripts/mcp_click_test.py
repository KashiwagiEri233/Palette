"""Connect to an existing owner through MCP stdio, observe and optionally click once."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from palette.config import find_config_root


async def probe(args: argparse.Namespace) -> int:
    root = (args.root or find_config_root()).resolve()
    output = args.out or root / ".runtime" / "acceptance" / uuid.uuid4().hex[:12]
    output.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "vm_id": args.vm_id,
        "transport": "MCP stdio -> existing owner",
        "application_effect": "requires visual verification",
        "calls": [],
        "ok": False,
    }
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "palette.cli", "--root", str(root), "serve", "--connect-only"],
        cwd=str(root),
    )
    try:
        async with asyncio.timeout(args.timeout):
            async with (
                stdio_client(parameters) as (reader, writer),
                ClientSession(reader, writer) as session,
            ):
                initialized = await session.initialize()
                report["server"] = initialized.server_info.model_dump(mode="json")
                listed = await session.list_tools()
                report["tools"] = sorted(tool.name for tool in listed.tools)
                required = {"vm_status", "vm_observe", "vm_act", "vm_wait"}
                if not required.issubset(report["tools"]):
                    raise RuntimeError("required Palette tools are missing")

                async def call(
                    name: str, arguments: dict[str, Any], stage: str
                ) -> tuple[dict[str, Any], bool]:
                    started = time.perf_counter()
                    result = await session.call_tool(name, arguments)
                    body = getattr(result, "structured_content", None)
                    if not isinstance(body, dict):
                        raise RuntimeError(f"{name} returned no structuredContent")
                    report["calls"].append(
                        {
                            "stage": stage,
                            "tool": name,
                            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                            "result": body,
                        }
                    )
                    for block in getattr(result, "content", []):
                        if block.type == "image":
                            image = base64.b64decode(block.data, validate=True)
                            if block.mime_type != "image/png" or not image.startswith(b"\x89PNG"):
                                raise RuntimeError("Palette returned an invalid PNG")
                            (output / f"{stage}.png").write_bytes(image)
                    failed = bool(getattr(result, "is_error", False)) or body.get("ok") is not True
                    return body, failed

                status, failed = await call("vm_status", {"vm_id": args.vm_id}, "01-status")
                if failed or status["status"]["state"] != "RUNNING":
                    raise RuntimeError("start the selected VM in Desktop before testing")
                observed, failed = await call("vm_observe", {"vm_id": args.vm_id}, "02-before")
                if failed:
                    raise RuntimeError(f"observation failed: {observed.get('error')}")
                if args.x is not None:
                    frame = observed["frame"]
                    if not (
                        0 <= args.x < frame["image_size"]["width"]
                        and 0 <= args.y < frame["image_size"]["height"]
                    ):
                        raise RuntimeError("click coordinates are outside the delivered image")
                    clicked, failed = await call(
                        "vm_act",
                        {
                            "vm_id": args.vm_id,
                            "instance_id": frame["instance_id"],
                            "frame_id": frame["frame_id"],
                            "request_id": "acceptance_" + uuid.uuid4().hex,
                            "action": {
                                "type": "click",
                                "x": args.x,
                                "y": args.y,
                                "count": args.count,
                                "button": args.button,
                            },
                        },
                        "03-click",
                    )
                    if args.expect_error:
                        code = (clicked.get("error") or {}).get("code")
                        if not failed or code != args.expect_error:
                            raise RuntimeError(f"expected {args.expect_error}, received {code}")
                        report["expected_error"] = code
                    elif failed or clicked.get("effect_state") != "submitted":
                        raise RuntimeError(
                            "click did not confirm success; do not retry blindly: "
                            + str(clicked.get("error"))
                        )
                    if not args.expect_error and args.wait_ms:
                        verified, failed = await call(
                            "vm_wait",
                            {
                                "vm_id": args.vm_id,
                                "mode": "delay",
                                "timeout_ms": args.wait_ms,
                                "delay_ms": args.wait_ms,
                            },
                            "04-verification",
                        )
                        if failed:
                            raise RuntimeError(
                                f"post-click observation failed: {verified.get('error')}"
                            )
                final, failed = await call("vm_status", {"vm_id": args.vm_id}, "05-status")
                if failed or final["status"]["instance_id"] != observed["frame"]["instance_id"]:
                    raise RuntimeError("the VM instance changed during the test")
                if args.x is not None and not args.expect_error:
                    ledger = final["status"]["input_ledger"]
                    if ledger["keys"] or ledger["buttons"] or not ledger["release_confirmed"]:
                        raise RuntimeError("held inputs remain after the click")
                    if final["status"]["action_seq"] <= observed["frame"]["action_seq"]:
                        raise RuntimeError("the click did not advance the action sequence")
                report["ok"] = True
    except Exception as exc:
        cause: BaseException = exc
        while isinstance(cause, BaseExceptionGroup) and len(cause.exceptions) == 1:
            cause = cause.exceptions[0]
        report["error"] = str(cause) or type(cause).__name__
    finally:
        (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"ok": report["ok"], "output": str(output), "error": report.get("error")}))
    return 0 if report["ok"] else 4


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--vm-id", required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--x", type=int)
    parser.add_argument("--y", type=int)
    parser.add_argument("--count", type=int, choices=[1, 2], default=1)
    parser.add_argument("--button", choices=["left", "right", "middle"], default="left")
    parser.add_argument("--wait-ms", type=int, default=1000)
    parser.add_argument("--expect-error", choices=["BUSY"])
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()
    if (args.x is None) != (args.y is None):
        parser.error("--x and --y must be supplied together")
    if args.expect_error and args.x is None:
        parser.error("--expect-error requires click coordinates")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if not 0 <= args.wait_ms <= 10000:
        parser.error("--wait-ms must be between 0 and 10000")
    return asyncio.run(probe(args))


if __name__ == "__main__":
    raise SystemExit(main())
