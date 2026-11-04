"""Observation-only D-Bus/QMP probe. Does not establish input/display acceptance.

The supplied profile must describe a disposable guest. --firmware creates a halted
firmware guest instead, with no desktop or visible input feedback claims.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import platform
import time
from pathlib import Path
from typing import Any

from palette.config import HostConfig, PaletteConfig, Profile, load_config
from palette.platforms import execution_info
from palette.service import PaletteService
from palette.timing import process_tree_metrics


def percentiles(values: list[float]) -> dict[str, float | int]:
    ordered = sorted(values)
    return {
        "count": len(values),
        **{f"p{p}": round(ordered[math.ceil(len(ordered) * p / 100) - 1], 3) for p in (50, 95, 99)},
    }


async def probe(args: argparse.Namespace) -> dict[str, Any]:
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    # Each invocation owns an independent runtime and never attaches to existing VMs.
    runtime = output.parent / f"display-probe-{time.time_ns()}"
    runtime.mkdir()
    if args.firmware:
        if args.qemu is None:
            raise ValueError("--firmware requires --qemu")
        base = runtime / "halt.raw"
        base.write_bytes(
            (b"\xfa\xf4\xeb\xfd".ljust(510, b"\0") + b"\x55\xaa").ljust(1024**2, b"\0")
        )
        qemu = args.qemu.resolve()
        profile = Profile.model_validate(
            {
                "profile_id": "probe",
                "qemu": {
                    "binary": str(qemu),
                    "qemu_img": str(qemu.with_name("qemu-img" + qemu.suffix)),
                    "accelerator": args.accelerator,
                    "cpu": "max" if args.accelerator == "tcg" else None,
                },
                "images": {"base": str(base), "format": "raw", "size_gib": 1},
                "resources": {"vcpus": 1, "memory_mib": 256, "process_memory_limit_mib": None},
                "network": {"mode": "offline"},
                "guest_tools": {"enabled": False},
            }
        )
    else:
        profile = load_config(args.root).profile(args.profile)
    config = PaletteConfig(
        profiles={profile.profile_id: profile},
        config_root=args.root.resolve(),
        host=HostConfig(
            default_profile=profile.profile_id,
            runtime_root=str(runtime / "runtime"),
            experimental_display="dbus",
            experimental_display_helper=str(args.helper.resolve()),
        ),
    )
    service = PaletteService(config)
    service.manager.console_enabled = True
    await service.startup()
    rows = []
    try:
        instance = await service.manager.start("probe")
        bridge = instance.display_bridge
        if bridge is None or await bridge.source.wait_frame(10) is None:
            raise RuntimeError("D-Bus helper did not produce a complete frame")
        if args.firmware:
            await asyncio.sleep(1)  # allow firmware video mode changes to complete
        for round_index in range(args.rounds):
            values: dict[str, list[float]] = {"qmp_ms": [], "shadow_ms": []}
            before = process_tree_metrics()
            started = time.monotonic()
            for sample in range(args.samples):
                # Alternate order to avoid always giving one provider a warmed cache.
                for provider in ("qmp", "shadow") if sample % 2 else ("shadow", "qmp"):
                    at = time.perf_counter()
                    if provider == "qmp":
                        await service.observe_image("probe")
                    else:
                        await bridge.source.shadow_capture(profile.capture)
                    values[f"{provider}_ms"].append((time.perf_counter() - at) * 1000)
            frame = bridge.source.frame
            if frame is None:
                raise RuntimeError("display source was lost during the probe")
            rows.append(
                {
                    "round": round_index + 1,
                    "resolution": [frame.width, frame.height],
                    "samples": values,
                    "summary": {name: percentiles(items) for name, items in values.items()},
                    "elapsed_seconds": time.monotonic() - started,
                    "resources_before": before,
                    "resources_after": process_tree_metrics(),
                }
            )
        return {
            "schema_version": 1,
            "kind": "observation-only",
            "execution": execution_info(profile),
            "host": platform.platform(),
            "guest": "halted x86 firmware" if args.firmware else profile.profile_id,
            "qemu_version": instance.qmp.version if instance.qmp else None,
            "rounds": rows,
            "input_to_visible_ms": None,
            "renderer_metrics": None,
            "migration_recommended": False,
            "limitations": [
                "No renderer or numbered guest input was measured.",
                "Both observation providers are active; this is not a production A/B resource comparison.",
            ],
        }
    finally:
        if service.manager.find("probe") is not None:
            await service.manager.stop("probe", force=True)
        await service.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--profile")
    parser.add_argument("--helper", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--firmware", action="store_true")
    parser.add_argument("--qemu", type=Path)
    parser.add_argument("--accelerator", choices=("tcg", "whpx", "kvm", "hvf"), default="tcg")
    args = parser.parse_args()
    if not 1 <= args.samples <= 10000 or not 1 <= args.rounds <= 20:
        parser.error("samples must be 1..10000 and rounds 1..20")
    report = asyncio.run(probe(args))
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"report": str(args.output.resolve()), "migration_recommended": False}))


if __name__ == "__main__":
    main()
