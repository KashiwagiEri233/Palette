"""Guest ISA selection must be independent of the host executing QEMU."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from palette import platforms
from palette.cli import _is_admin, build_doctor_report
from palette.config import HostConfig, Profile, QemuConfig, TimeoutConfig, load_config
from palette.errors import PaletteError
from palette.vm.launcher import build_launch_plan, locate_binary, resolve_firmware


@pytest.mark.parametrize(
    "system,host,guest,accel,allowed",
    [
        ("win32", "AMD64", "x86_64", "whpx", True),
        ("darwin", "arm64", "aarch64", "hvf", True),
        ("darwin", "x86_64", "x86_64", "hvf", True),
        ("linux", "x86_64", "x86_64", "kvm", True),
        ("linux", "aarch64", "aarch64", "kvm", True),
        ("darwin", "arm64", "x86_64", "tcg", True),
        ("linux", "x86_64", "aarch64", "tcg", True),
        ("win32", "AMD64", "aarch64", "tcg", True),
        ("darwin", "arm64", "x86_64", "hvf", False),
        ("linux", "x86_64", "aarch64", "kvm", False),
        ("linux", "x86_64", "x86_64", "whpx", False),
    ],
)
def test_target_matrix(monkeypatch, profile, system, host, guest, accel, allowed):
    monkeypatch.setattr(platforms.sys, "platform", system)
    monkeypatch.setattr(platforms.platform, "machine", lambda: host)
    target = profile.model_copy(update={"qemu": QemuConfig(arch=guest, accelerator=accel)})
    if allowed:
        platforms.validate_target(target)
        info = platforms.execution_info(target)
        assert info["guest_arch"] == guest
        assert info["cross_architecture"] == (platforms.host_architecture() != guest)
    else:
        with pytest.raises(PaletteError, match="hardware acceleration"):
            platforms.validate_target(target)


@pytest.mark.parametrize(
    "system,host,selected",
    [
        ("win32", "AMD64", "default"),
        ("darwin", "arm64", "macos-aarch64"),
        ("darwin", "x86_64", "macos-x86_64"),
        ("linux", "aarch64", "linux-aarch64"),
        ("linux", "x86_64", "linux-x86_64"),
    ],
)
def test_auto_profile_uses_native_host(monkeypatch, system, host, selected):
    monkeypatch.setattr(platforms.sys, "platform", system)
    monkeypatch.setattr(platforms.platform, "machine", lambda: host)
    config = load_config(Path(__file__).resolve().parents[2])
    assert config.profile(None).profile_id == selected
    assert config.profile("tcg-x86_64").qemu.accelerator == "tcg"


def test_tcg_and_target_defaults():
    arm = QemuConfig(arch="aarch64", accelerator="tcg")
    assert (arm.binary, arm.machine, arm.cpu) == ("qemu-system-aarch64", "virt", "max")
    with pytest.raises(ValidationError, match="cpu=host"):
        QemuConfig(accelerator="tcg", cpu="host")
    with pytest.raises(ValidationError, match="virt"):
        QemuConfig(arch="aarch64", machine="q35")
    with pytest.raises(ValidationError):
        Profile.model_validate({"qemu": None, "images": {"base": "disk"}})


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_timeout_budgets_must_be_positive_and_finite(timeout):
    with pytest.raises(ValidationError):
        TimeoutConfig(startup_seconds=timeout)


@pytest.mark.parametrize("profile_id", ["tcg-x86_64", "tcg-aarch64"])
def test_tcg_profiles_have_independent_budgets(profile_id):
    profile = load_config(Path(__file__).resolve().parents[2]).profile(profile_id)
    assert profile.qemu.cpu == "max"
    assert profile.timeouts.startup_seconds == 600
    assert profile.timeouts.qmp_handshake_seconds == 60
    assert profile.timeouts.qmp_command_seconds == 30
    assert profile.capture.timeout_seconds == 30
    assert profile.resources.max_action_seconds == 60
    plan = build_launch_plan(
        profile=profile,
        binary=profile.qemu.binary,
        vm_id="v",
        instance_id="i",
        overlay_path=Path("overlay"),
        workdir=Path("."),
    )
    if profile.qemu.arch == "aarch64":
        assert "model=virtio-net-pci" in plan.argv[plan.argv.index("-nic") + 1]


def test_arm_device_graph_is_independent_of_host(profile, tmp_path):
    data = profile.model_dump()
    data["qemu"] = {"arch": "aarch64", "accelerator": "tcg"}
    del data["devices"]
    target = Profile.model_validate(data)
    plan = build_launch_plan(
        profile=target,
        binary=target.qemu.binary,
        instance_id="i",
        vm_id="v",
        overlay_path=tmp_path / "overlay",
        workdir=tmp_path,
    )
    assert plan.argv[plan.argv.index("-machine") + 1] == "virt"
    assert "virtio-gpu-pci,id=video0" in plan.argv
    assert "qemu-xhci,id=xhci" in plan.argv
    assert "virtio-blk-pci,drive=disk,bootindex=0" in plan.argv
    assert "VGA,id=video0" not in plan.argv


def test_missing_target_binary_never_falls_back(monkeypatch, profile, tmp_path):
    (tmp_path / "qemu-system-aarch64").touch()
    monkeypatch.setattr("palette.vm.launcher.shutil.which", lambda _: None)
    target = profile.model_copy(update={"qemu": QemuConfig(accelerator="tcg")})
    assert locate_binary(target, HostConfig(qemu_search_paths=[str(tmp_path)])) is None
    wrong = target.model_copy(
        update={"qemu": QemuConfig(binary="qemu-system-aarch64", accelerator="tcg")}
    )
    with pytest.raises(PaletteError, match="does not match"):
        locate_binary(wrong, HostConfig())


def test_firmware_discovery_and_arch_rejection(profile, tmp_path):
    binary = tmp_path / "bin" / "qemu-system-aarch64"
    share = tmp_path / "share" / "qemu"
    share.mkdir(parents=True)
    for name in ("edk2-aarch64-code.fd", "edk2-arm-vars.fd"):
        (share / name).write_bytes(b"firmware")
    target = profile.model_copy(update={"qemu": QemuConfig(arch="aarch64", accelerator="tcg")})
    resolved = resolve_firmware(target, str(binary))
    assert Path(resolved.qemu.firmware.code).parent == share
    assert target.qemu.firmware.code is None
    wrong = target.model_copy(
        update={
            "qemu": target.qemu.model_copy(
                update={
                    "firmware": resolved.qemu.firmware.model_copy(
                        update={"code": str(share / "OVMF_CODE.fd")}
                    )
                }
            )
        }
    )
    with pytest.raises(PaletteError, match="does not match"):
        resolve_firmware(wrong, str(binary))


def test_trusted_relative_paths_use_config_root(tmp_path, monkeypatch):
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "default.toml").write_text(
        '[qemu]\naccelerator="tcg"\n[qemu.firmware]\ncode="firmware/code.fd"\n'
        '[images]\nbase="images/base.qcow2"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path.parent)
    config = load_config(tmp_path)
    assert config.profile(None).images.base == str(tmp_path / "images" / "base.qcow2")
    assert config.profile(None).qemu.firmware.code == str(tmp_path / "firmware" / "code.fd")


def test_posix_admin_means_uid_zero(monkeypatch):
    monkeypatch.setattr("palette.cli.IS_WINDOWS", False)
    monkeypatch.setattr("palette.cli.os.geteuid", lambda: 0, raising=False)
    assert _is_admin()
    monkeypatch.setattr("palette.cli.os.geteuid", lambda: 1000)
    assert not _is_admin()


def test_doctor_reports_cross_arch_even_without_qemu(monkeypatch):
    monkeypatch.setattr(platforms.sys, "platform", "darwin")
    monkeypatch.setattr(platforms.platform, "machine", lambda: "arm64")
    monkeypatch.setattr("palette.cli.locate_binary", lambda *args: None)
    config = load_config(Path(__file__).resolve().parents[2])
    report = build_doctor_report(config, profile_id="tcg-x86_64")
    assert report["execution"]["cross_architecture"]
    assert report["execution"]["memory_enforcement"] == "sampled-rss"
    assert next(c for c in report["checks"] if c["name"] == "target_compatibility")["ok"]
    assert not report["ok"]
