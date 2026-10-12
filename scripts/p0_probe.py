"""P0 probe: start -> screenshot -> one click -> screenshot again.

The plan's first milestone, and the first thing to run on a new host. It exercises the
whole critical path end to end and prints the numbers the plan asks for, separated the way
the plan asks for them:

    start to QMP ready / start to first valid framebuffer
    observe latency, and the split between capture, encode and delivery
    click latency including the post-action capture

Usage::

    uv run python scripts/p0_probe.py --profile default --vm-id probe
    uv run python scripts/p0_probe.py --profile default --x 400 --y 300 --out ./.runtime/probe
    uv run python scripts/p0_probe.py --help

Exit codes: 0 on success, 3 when the host is not ready (see ``palette doctor``), 4 when the
run failed. Nothing here is a security control: it is an operator tool that uses the same
service the MCP server does, with the same policy.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path

from palette.audit import AuditLog
from palette.config import find_config_root, load_config
from palette.errors import ErrorCode, PaletteError
from palette.service import PaletteService

NOT_READY = 3
FAILED = 4


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Palette P0 probe: see, click, see.")
    parser.add_argument("--root", type=Path, default=None, help="repository root holding profiles/")
    parser.add_argument(
        "--profile", default=None, help="profile id (default: the configured default)"
    )
    parser.add_argument("--vm-id", default="probe")
    parser.add_argument("--x", type=int, default=None, help="click coordinate (default: centre)")
    parser.add_argument("--y", type=int, default=None)
    parser.add_argument("--text", default=None, help="also type this text after the click")
    parser.add_argument("--out", type=Path, default=None, help="directory to write the frames to")
    parser.add_argument(
        "--keep-running", action="store_true", help="leave the VM running afterwards"
    )
    return parser.parse_args()


async def probe(args: argparse.Namespace) -> int:
    config = load_config(args.root or find_config_root())
    profile = config.profile(args.profile)
    runtime = config.runtime_root
    audit = AuditLog(
        runtime / "audit" / "palette.jsonl",
        max_bytes=profile.retention.audit_max_bytes,
        max_files=profile.retention.audit_max_files,
        enabled=config.host.audit_enabled,
    )
    service = PaletteService(config, audit=audit)
    await service.startup()

    out_dir = args.out or (runtime / "probe")
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        started = time.perf_counter()
        print(f"* starting {args.vm_id!r} from profile {profile.profile_id!r} ...")
        instance = await service.manager.start(args.vm_id, profile.profile_id)
        so_far = time.perf_counter() - started
        print(
            f"  state={instance.state} qmp_ready={instance.view.qmp_ready} "
            f"screen_ready={instance.view.screen_ready} desktop_ready={instance.view.desktop_ready}"
        )
        print(f"  start -> running: {so_far * 1000:.0f} ms")
        if instance.view.native_size is not None:
            size = instance.view.native_size
            print(f"  framebuffer: {size.width}x{size.height}")
        if not instance.view.screen_ready:
            print(
                "FAIL: no valid framebuffer was captured; check `palette vm status` and the guest"
            )
            return FAILED

        before = time.perf_counter()
        observed, image = await service.observe_image(args.vm_id)
        observe_ms = (time.perf_counter() - before) * 1000
        assert observed.frame is not None
        frame = observed.frame
        (out_dir / "01-before.png").write_bytes(image)
        print(
            f"* observe: {observe_ms:.0f} ms, {observed.image_bytes} bytes, "
            f"{frame.image_size.width}x{frame.image_size.height} "
            f"(native {frame.native_size.width}x{frame.native_size.height}, "
            f"scaled={observed.scaled})"
        )

        x = args.x if args.x is not None else frame.image_size.width // 2
        y = args.y if args.y is not None else frame.image_size.height // 2
        if not (0 <= x < frame.image_size.width and 0 <= y < frame.image_size.height):
            print(
                f"FAIL: click ({x}, {y}) is outside the {frame.image_size.width}x"
                f"{frame.image_size.height} image"
            )
            return FAILED

        before = time.perf_counter()
        receipt, after = await service.act(
            args.vm_id,
            instance_id=instance.view.instance_id,
            frame_id=frame.frame_id,
            request_id="p0-probe-click",
            action={"type": "click", "x": x, "y": y},
        )
        click_ms = (time.perf_counter() - before) * 1000
        print(
            f"* click at ({x}, {y}): ok={receipt.ok} effect_state={receipt.effect_state} "
            f"observation={receipt.observation} in {click_ms:.0f} ms"
        )
        if receipt.error is not None:
            print(f"  error: {receipt.error.code}: {receipt.error.message}")
        if after is not None:
            (out_dir / "02-after-click.png").write_bytes(after)

        if args.text:
            target = receipt.frame or frame
            typed, typed_image = await service.act(
                args.vm_id,
                instance_id=instance.view.instance_id,
                frame_id=target.frame_id,
                request_id="p0-probe-text",
                action={"type": "type_text", "text": args.text, "mode": "keyboard"},
            )
            print(
                f"* type_text({args.text!r}): ok={typed.ok} "
                f"effect_state={typed.effect_state} backend={typed.backend}"
            )
            if typed.error is not None:
                print(f"  error: {typed.error.code}: {typed.error.message}")
            if typed_image is not None:
                (out_dir / "03-after-text.png").write_bytes(typed_image)

        status = await service.status(args.vm_id)
        if status.status is not None:
            held = status.status.input_ledger
            print(f"* input ledger: keys={held.keys} buttons={held.buttons}")
        print(f"* frames written to {out_dir}")
        print("OK: the service saw the desktop and the input reached it")
        print("  This is not a desktop-readiness verdict: check the screenshots yourself.")
        if not args.keep_running:
            await service.manager.stop(args.vm_id)
            print("* VM stopped")
        return 0
    except PaletteError as exc:
        code = (
            NOT_READY
            if exc.code
            in {
                ErrorCode.QEMU_NOT_FOUND,
                ErrorCode.IMAGE_MISSING,
                ErrorCode.ACCELERATION_UNAVAILABLE,
                ErrorCode.IMAGE_INVALID,
                ErrorCode.PRECONDITION_FAILED,
            }
            else FAILED
        )
        print(f"FAIL: {exc.code}: {exc.message}")
        if exc.details:
            print(f"  details: {exc.details}")
        if code == NOT_READY:
            print("  run `palette doctor` first")
        return code
    finally:
        if not args.keep_running:
            await service.shutdown()


def main() -> int:
    # A Windows console may use a legacy code page that cannot encode the progress
    # markers; ASCII output keeps the tool usable everywhere.
    return asyncio.run(probe(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
