"""Operator CLI: ``doctor``, ``serve`` and the lifecycle commands.

Everything here runs with the trusted ``operator`` role. The CLI exists so that the
agent never *needs* lifecycle authority: starting, stopping and resetting a VM is an
operator action by default, and the plan requires an operator-side emergency stop that
works even while an agent believes it owns the VM.

Logging always goes to stderr. On stdio transport stdout belongs to the MCP protocol, and
a stray log line there would corrupt the session.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import logging
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__
from .audit import AuditLog
from .config import PaletteConfig, find_config_root, load_config
from .contracts import AgentService
from .errors import ErrorCode, PaletteError
from .operator import OperatorClient
from .platforms import execution_info, validate_target
from .policy import PathGuard
from .server import build_server
from .service import PaletteService
from .vm.launcher import locate_binary, probe_binary, resolve_firmware, run_sync

logger = logging.getLogger("palette.cli")

#: See the note in ``vm/process_guard.py``: a plain bool, because mypy narrows ``sys.platform``
#: to the checking machine's value and would then reject the other platform's branch.
IS_WINDOWS = sys.platform == "win32"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_PRECONDITION = 3
EXIT_RUNTIME = 4

DEFAULT_VM_ID = "desktop"


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _configure_logging(getattr(args, "log_level", "INFO"))

    try:
        return int(args.handler(args))
    except PaletteError as exc:
        _print_error(exc)
        return (
            EXIT_PRECONDITION
            if exc.code
            in {
                ErrorCode.QEMU_NOT_FOUND,
                ErrorCode.IMAGE_MISSING,
                ErrorCode.IMAGE_INVALID,
                ErrorCode.ACCELERATION_UNAVAILABLE,
                ErrorCode.POLICY_DENIED,
            }
            else EXIT_RUNTIME
        )
    except KeyboardInterrupt:  # pragma: no cover - interactive
        print("\ninterrupted", file=sys.stderr)
        return EXIT_RUNTIME


# --------------------------------------------------------------------------------------
# argument parsing
# --------------------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="palette",
        description="Operate a QEMU virtual desktop for visual agents.",
    )
    parser.add_argument("--version", action="version", version=f"palette {__version__}")
    parser.add_argument("--root", type=Path, default=None, help="repository root holding profiles/")
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="probe the host, QEMU and the configured profile")
    doctor.add_argument("--profile", default=None)
    doctor.add_argument("--json", action="store_true", help="emit the report as JSON")
    doctor.add_argument(
        "--hash-base", action="store_true", help="also hash the base image (reads the whole file)"
    )
    doctor.set_defaults(handler=_cmd_doctor)

    serve = sub.add_parser("serve", help="run the MCP server on stdio")
    serve.add_argument("--profile", default=None, help="default profile for lifecycle commands")
    serve.add_argument(
        "--standalone", action="store_true", help="own this runtime until stdio closes"
    )
    serve.add_argument(
        "--connect-only", action="store_true", help="only connect to an existing owner"
    )
    serve.set_defaults(handler=_cmd_serve)

    daemon = sub.add_parser("daemon", help="manage the shared desktop and MCP owner")
    daemon_sub = daemon.add_subparsers(dest="daemon_command", required=True)
    for action in ("run", "start", "status", "stop"):
        operation = daemon_sub.add_parser(action)
        operation.set_defaults(handler=_cmd_daemon)

    config = sub.add_parser("config", help="inspect trusted configuration")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    show = config_sub.add_parser("show", help="print the resolved configuration")
    show.set_defaults(handler=_cmd_config_show)

    vm = sub.add_parser("vm", help="operator lifecycle commands")
    vm_sub = vm.add_subparsers(dest="vm_command", required=True)

    start = vm_sub.add_parser("start", help="create an instance and boot it")
    start.add_argument("--vm-id", default=DEFAULT_VM_ID)
    start.add_argument("--profile", default=None)
    start.add_argument(
        "--image",
        default=None,
        help="base image to back this instance instead of the profile's pinned one",
    )
    start.add_argument("--image-format", default=None, choices=["qcow2", "raw"])
    start.add_argument(
        "--iso",
        action="append",
        default=None,
        help="attach a CD-ROM; repeatable",
    )
    start.add_argument(
        "--disk",
        action="append",
        default=None,
        help="attach an extra disk; repeatable",
    )
    start.add_argument(
        "--qemu-arg",
        action="append",
        default=None,
        help=(
            "append a raw QEMU argument; repeatable. You are handing QEMU to the caller: "
            "arguments can read and write host files and add a second control channel"
        ),
    )
    start.set_defaults(handler=_cmd_vm_start)

    stop = vm_sub.add_parser("stop", help="shut the guest down and stop QEMU")
    stop.add_argument("--vm-id", default=DEFAULT_VM_ID)
    stop.add_argument("--force", action="store_true", help="kill QEMU instead of asking the guest")
    stop.set_defaults(handler=_cmd_vm_stop)

    reset = vm_sub.add_parser("reset", help="discard the writable layer and rebuild from the base")
    reset.add_argument("--vm-id", default=DEFAULT_VM_ID)
    reset.add_argument("--profile", default=None)
    reset.set_defaults(handler=_cmd_vm_reset)

    status = vm_sub.add_parser("status", help="show lifecycle and readiness")
    status.add_argument("--vm-id", default=DEFAULT_VM_ID)
    status.set_defaults(handler=_cmd_vm_status)

    emergency = vm_sub.add_parser("emergency-stop", help="release inputs and kill QEMU now")
    emergency.add_argument("--vm-id", default=None, help="omit to stop every managed VM")
    emergency.set_defaults(handler=_cmd_vm_emergency_stop)

    take = vm_sub.add_parser("take-control", help="bump the control epoch and retire all frames")
    take.add_argument("--vm-id", default=DEFAULT_VM_ID)
    take.add_argument("--reason", default="operator takeover")
    take.set_defaults(handler=_cmd_vm_take_control)

    list_vms = vm_sub.add_parser("list", help="list managed instances on disk")
    list_vms.add_argument("--vm-id", default=None)
    list_vms.set_defaults(handler=_cmd_vm_list)

    return parser


def _configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level, logging.INFO))


# --------------------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------------------


def _cmd_doctor(args: argparse.Namespace) -> int:
    config = _load(args)
    report = build_doctor_report(
        config,
        profile_id=args.profile,
        hash_base=args.hash_base,
    )
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print_report(report)
    return EXIT_OK if report["ok"] else EXIT_PRECONDITION


def _cmd_serve(args: argparse.Namespace) -> int:
    from .audit import NullAuditLog

    config = _load(args)
    if args.profile:
        # A default-profile override changes only which profile bare lifecycle commands use.
        config = config.model_copy(
            update={"host": config.host.model_copy(update={"default_profile": args.profile})}
        )
    runtime = config.runtime_root
    audit = (
        AuditLog(
            runtime / "audit" / "palette.jsonl",
            max_bytes=config.profile(None).retention.audit_max_bytes,
            max_files=config.profile(None).retention.audit_max_files,
            enabled=config.host.audit_enabled,
        )
        if config.host.audit_enabled
        else NullAuditLog()
    )
    service: AgentService = PaletteService(config, audit=audit)
    if not args.standalone:
        from .agent_ipc import RemoteService, discover
        from .operator.protocol import Endpoint

        owner_info = asyncio.run(discover(config))
        if owner_info is not None:
            service = RemoteService(
                config,
                Endpoint.model_validate(owner_info["agent_endpoint"]),
                default_profile=args.profile,
            )
        elif args.connect_only:
            raise PaletteError(ErrorCode.PRECONDITION_FAILED, "no running palette owner")
    elif args.connect_only:
        raise PaletteError(
            ErrorCode.INVALID_ARGUMENT, "standalone and connect-only are mutually exclusive"
        )
    server = build_server(service)
    logger.info("palette %s serving MCP on stdio", __version__)
    try:
        server.run("stdio")
    except ExceptionGroup as group:
        error: BaseException = group
        while isinstance(error, BaseExceptionGroup) and len(error.exceptions) == 1:
            error = error.exceptions[0]
        if isinstance(error, PaletteError):
            raise error from None
        raise
    return EXIT_OK


def _cmd_daemon(args: argparse.Namespace) -> int:
    from .agent_ipc import discover
    from .runtime import RuntimeOwner

    config = _load(args)
    if args.daemon_command == "status":
        info = asyncio.run(discover(config))
        if info is None:
            raise PaletteError(ErrorCode.PRECONDITION_FAILED, "no running palette owner")
        info.pop("agent_endpoint", None)
        print(json.dumps(info, indent=2))
        return EXIT_OK
    if args.daemon_command == "stop":
        print(json.dumps(asyncio.run(OperatorClient(config.runtime_root).call("daemon.stop"))))
        return EXIT_OK
    if args.daemon_command == "start":
        if asyncio.run(discover(config)) is not None:
            return EXIT_OK
        from .vm.launcher import spawn_daemon

        spawn_daemon(config.config_root)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if asyncio.run(discover(config)) is not None:
                return EXIT_OK
            time.sleep(0.1)
        raise PaletteError(ErrorCode.TIMEOUT, "daemon did not publish a ready endpoint")

    async def run() -> None:
        owner = RuntimeOwner(
            PaletteService(
                config,
                audit=AuditLog(
                    config.runtime_root / "audit" / "palette.jsonl",
                    enabled=config.host.audit_enabled,
                ),
            ),
            kind="daemon",
        )
        loop = asyncio.get_running_loop()
        if not IS_WINDOWS:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, owner.stopping.set)
        try:
            await owner.start()
            await owner.stopping.wait()
        finally:
            await owner.close()

    asyncio.run(run())
    return EXIT_OK


def _cmd_config_show(args: argparse.Namespace) -> int:
    config = _load(args)
    print(
        json.dumps(
            {
                "config_root": str(config.config_root),
                "runtime_root": str(config.runtime_root),
                "default_profile": config.host.default_profile,
                "profiles": {
                    profile_id: profile.model_dump(mode="json")
                    for profile_id, profile in config.profiles.items()
                },
            },
            indent=2,
            default=str,
        )
    )
    return EXIT_OK


def _cmd_vm_start(args: argparse.Namespace) -> int:
    return _operator_command(
        args,
        "start",
        vm_id=args.vm_id,
        profile_id=args.profile,
        image=args.image,
        image_format=args.image_format,
        iso=args.iso,
        extra_disks=args.disk,
        qemu_args=args.qemu_arg,
    )


def _cmd_vm_stop(args: argparse.Namespace) -> int:
    return _operator_command(args, "stop", vm_id=args.vm_id, force=args.force)


def _cmd_vm_reset(args: argparse.Namespace) -> int:
    return _operator_command(args, "reset", vm_id=args.vm_id, profile_id=args.profile)


def _cmd_vm_status(args: argparse.Namespace) -> int:
    return _operator_command(args, "status", vm_id=args.vm_id)


def _cmd_vm_emergency_stop(args: argparse.Namespace) -> int:
    return _operator_command(args, "emergency_stop", vm_id=args.vm_id)


def _cmd_vm_take_control(args: argparse.Namespace) -> int:
    return _operator_command(args, "take_control", vm_id=args.vm_id, reason=args.reason)


def _operator_command(args: argparse.Namespace, method: str, **parameters: Any) -> int:
    result = asyncio.run(_service(args).call(method, **parameters))
    print(json.dumps(result, indent=2, default=str))
    return EXIT_OK if result.get("ok", True) else EXIT_RUNTIME


def _cmd_vm_list(args: argparse.Namespace) -> int:
    config = _load(args)
    from .vm.images import ImageManager

    profile = config.profile(None)
    images = ImageManager(PathGuard(config.runtime_root), profile)
    if args.vm_id:
        vm_ids = [args.vm_id]
    else:
        vms_root = config.runtime_root / "vms"
        vm_ids = (
            sorted(child.name for child in vms_root.iterdir() if child.is_dir())
            if vms_root.is_dir()
            else []
        )
    payload = {vm_id: images.list_instances(vm_id) for vm_id in vm_ids}
    print(json.dumps(payload, indent=2, default=str))
    return EXIT_OK


# --------------------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------------------


def build_doctor_report(
    config: PaletteConfig, *, profile_id: str | None = None, hash_base: bool = False
) -> dict[str, Any]:
    """Collect everything P0 needs to know, and refuse to guess at the rest.

    A missing QEMU binary is reported as a hard failure. It is never papered over with a
    TCG fallback or a "probably fine" message, because a report that cannot distinguish
    "runs accelerated" from "runs at all" is not worth printing.
    """
    profile = config.profile(profile_id)
    report: dict[str, Any] = {
        "palette_version": __version__,
        "execution": execution_info(profile),
        "ok": True,
        "host": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": sys.version.split()[0],
            "executable": sys.executable,
            "is_admin": _is_admin(),
        },
        "config": {
            "root": str(config.config_root),
            "runtime_root": str(config.runtime_root),
            "default_profile": config.host.default_profile,
            "profiles": config.profile_ids(),
        },
        "profile": {
            "profile_id": profile.profile_id,
            "mode": profile.mode,
            "accelerator_requested": profile.qemu.accelerator,
            "machine": profile.qemu.machine,
            "display": f"{profile.display.width}x{profile.display.height}",
            "keymap": profile.display.keymap,
            "network": profile.network.mode,
            "devices": profile.devices.model_dump(),
            "resources": profile.resources.model_dump(),
            "lifecycle_permissions": profile.lifecycle.model_dump(),
            "guest_tools_enabled": profile.guest_tools.enabled,
            "guest_tools_capabilities": sorted(profile.guest_tools.capabilities),
        },
        "checks": [],
    }
    checks: list[dict[str, Any]] = report["checks"]

    def check(name: str, ok: bool, detail: Any, *, blocking: bool = True) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail, "blocking": blocking})
        if blocking and not ok:
            report["ok"] = False

    try:
        validate_target(profile)
        check("target_compatibility", True, execution_info(profile))
    except PaletteError as exc:
        check("target_compatibility", False, exc.message)
    try:
        binary = locate_binary(profile, config.host)
    except PaletteError as exc:
        check("qemu_architecture", False, exc.message)
        binary = None
    check("qemu_binary", binary is not None, binary or "not found on PATH or in the search paths")
    if binary is None:
        report["acceleration"] = {"available": [], "note": "no QEMU binary to probe"}
        report["base_image"] = {"ok": False, "reason": "skipped: no QEMU binary"}
        report["base_image_ok"] = False
        return report

    probe = probe_binary(binary, with_devices=True, prefix=profile.qemu.argv_prefix)
    try:
        profile = resolve_firmware(profile, binary)
        check("firmware", True, profile.qemu.firmware.model_dump())
    except PaletteError as exc:
        check("firmware", False, exc.message)
    report["qemu"] = probe.as_dict()
    check("qemu_version", probe.found, probe.version or probe.error)
    if profile.qemu.version_pin and probe.version:
        check(
            "qemu_version_pin",
            probe.version.startswith(profile.qemu.version_pin),
            f"installed {probe.version}, pinned {profile.qemu.version_pin}",
        )
    report["acceleration"] = {
        "available": probe.accelerators,
        "requested": profile.qemu.accelerator,
        "whpx_api_dll": _whpx_dll_present() if IS_WINDOWS else None,
        "note": (
            "the accelerator list proves the build knows about it; only a real start proves "
            "this host can use it"
        ),
    }
    check(
        "accelerator",
        (not probe.accelerators) or profile.qemu.accelerator in probe.accelerators,
        f"requested {profile.qemu.accelerator}, build offers {probe.accelerators or 'unknown'}",
    )
    check(
        "explicit_acceleration",
        profile.qemu.accelerator != "tcg",
        (
            "profile selects TCG; QEMU offers no guest isolation guarantee for that mode"
            if profile.qemu.accelerator == "tcg"
            else f"profile selects {profile.qemu.accelerator}"
        ),
        blocking=False,
    )
    for device in (
        profile.devices.display_model,
        "qemu-xhci",
        "usb-kbd",
        "usb-tablet",
        "virtio-blk-pci",
    ):
        check(
            f"device:{device}",
            probe.supports_device(device),
            "listed by -device help"
            if probe.supports_device(device)
            else "not found in -device help",
        )
    check(
        "not_elevated",
        not _is_admin(),
        "QEMU should not run as administrator"
        if not _is_admin()
        else "this shell is elevated; run Palette as a normal user",
        blocking=False,
    )

    image_info = _probe_base_image(profile, hash_base=hash_base)
    report["base_image"] = image_info
    check(
        "base_image",
        bool(image_info.get("ok")),
        image_info.get("reason") or image_info.get("configured"),
    )

    runtime = config.runtime_root
    report["runtime"] = {
        "path": str(runtime),
        "writable": _is_writable(runtime),
        "free_bytes": _free_bytes(runtime),
        "qemu_img": _probe_qemu_img(profile),
    }
    check("runtime_writable", report["runtime"]["writable"], str(runtime))
    check(
        "qemu_img",
        report["runtime"]["qemu_img"]["found"],
        report["runtime"]["qemu_img"].get("path")
        or "qemu-img not found; overlays cannot be created",
    )
    return report


def _probe_base_image(profile: Any, *, hash_base: bool) -> dict[str, Any]:
    from .vm.images import ImageManager

    guard = PathGuard(Path(os.getcwd()))
    manager = ImageManager(guard, profile)
    try:
        return manager.verify_base_image(compute_hash=hash_base)
    except PaletteError as exc:
        return {"ok": False, "reason": exc.message, "configured": profile.images.base}


def _probe_qemu_img(profile: Any) -> dict[str, Any]:
    from .vm.images import resolve_qemu_img

    path = resolve_qemu_img(profile)
    info: dict[str, Any] = {"found": path is not None, "path": path}
    if path:
        try:
            result = run_sync([path, *profile.qemu.qemu_img_prefix, "--version"], timeout=20.0)
            info["version"] = (
                (result.stdout or "").strip().splitlines()[0] if result.stdout else None
            )
        except (OSError, subprocess.SubprocessError) as exc:
            info["error"] = type(exc).__name__
    return info


def _whpx_dll_present() -> bool:
    system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    return os.path.isfile(os.path.join(system_root, "System32", "WinHvPlatform.dll"))


def _is_admin() -> bool:
    """Whether this process is elevated.

    Both branches are looked up by name rather than written as attributes: ``ctypes.windll``
    and ``os.geteuid`` each exist on only one platform, and the type checker sees whichever
    platform it is running *on*, not the one the branch is for.
    """
    if IS_WINDOWS:
        windll = getattr(ctypes, "windll", None)
        if windll is None:  # pragma: no cover - a Windows interpreter always has it
            return False
        try:
            return bool(windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    geteuid = getattr(os, "geteuid", None)
    return geteuid() == 0 if geteuid is not None else False


def _is_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".palette-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _free_bytes(path: Path) -> int | None:
    try:
        return shutil.disk_usage(path if path.exists() else path.parent).free
    except OSError:
        return None


def _print_report(report: dict[str, Any]) -> None:
    print(f"palette {report['palette_version']} doctor")
    print(f"  host      : {report['host']['platform']} ({report['host']['machine']})")
    print(f"  python    : {report['host']['python']}")
    print(f"  root      : {report['config']['root']}")
    print(f"  runtime   : {report['config']['runtime_root']}")
    print(f"  profile   : {report['profile']['profile_id']} ({report['profile']['mode']})")
    target = report["execution"]
    print(
        f"  target    : {target['host_arch']} -> {target['guest_arch']} / {target['accelerator']}"
    )
    print(
        f"  memory cap: {target['memory_enforcement']} (sample interval: {target['memory_sample_interval_ms']} ms)"
    )
    checked = report.get("checks", [])
    print("  checks    :")
    for entry in checked:
        marker = "PASS" if entry["ok"] else ("FAIL" if entry["blocking"] else "WARN")
        print(f"    [{marker}] {entry['name']}: {entry['detail']}")
    qemu = report.get("qemu")
    if qemu:
        print(f"  qemu      : {qemu.get('version') or qemu.get('error')}")
        print(f"  accelerate: {report['acceleration']['available']}")
    print(f"  result    : {'ready' if report['ok'] else 'NOT READY'}")


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def _load(args: argparse.Namespace) -> PaletteConfig:
    root = args.root or find_config_root()
    return load_config(root)


def _service(args: argparse.Namespace) -> OperatorClient:
    return OperatorClient(_load(args).runtime_root)


def _print_error(exc: PaletteError) -> None:
    print(f"{exc.code}: {exc.message}", file=sys.stderr)
    if exc.details:
        print(f"  details: {json.dumps(exc.details, default=str)}", file=sys.stderr)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
