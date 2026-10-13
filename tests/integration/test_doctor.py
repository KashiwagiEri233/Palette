"""``palette doctor``: what it reports, and what it refuses to paper over.

The report is the P0 gate, so it has to be honest in both directions: it must pass for a
host that is genuinely ready, and it must block on a host that is not — without offering a
TCG fallback as an alternative to fixing the problem.
"""

from __future__ import annotations

import json
from pathlib import Path

from palette.cli import main
from palette.config import HostConfig, PaletteConfig
from palette.vm.launcher import probe_binary
from support.profiles import FAKE_QEMU, make_profile


def fake_config(base_image: Path, tmp_path: Path, **host_overrides) -> PaletteConfig:
    profile = make_profile(base_image)
    host = HostConfig(
        runtime_root=str(tmp_path / "runtime"),
        default_profile="default",
        qemu_search_paths=[],
        **host_overrides,
    )
    return PaletteConfig(profiles={"default": profile}, config_root=tmp_path).model_copy(
        update={"host": host}
    )


class TestAgainstTheTestDouble:
    def test_a_ready_host_passes_every_check(self, base_image: Path, tmp_path: Path) -> None:
        from palette.cli import build_doctor_report

        report = build_doctor_report(fake_config(base_image, tmp_path))
        failures = [check for check in report["checks"] if not check["ok"] and check["blocking"]]
        assert failures == []
        assert report["ok"] is True

    def test_the_report_names_the_binary_the_accelerators_and_the_devices(
        self, base_image: Path, tmp_path: Path
    ) -> None:
        from palette.cli import build_doctor_report

        report = build_doctor_report(fake_config(base_image, tmp_path))
        assert report["qemu"]["found"] is True
        assert report["qemu"]["version"] == "11.1.2"
        assert "whpx" in report["acceleration"]["available"]
        assert report["runtime"]["qemu_img"]["found"] is True
        checked = {check["name"] for check in report["checks"]}
        assert "device:usb-tablet" in checked
        assert "device:VGA" in checked

    def test_the_base_image_is_reported_as_unverified_without_a_pinned_digest(
        self, base_image: Path, tmp_path: Path
    ) -> None:
        from palette.cli import build_doctor_report

        report = build_doctor_report(fake_config(base_image, tmp_path))
        assert report["base_image"]["ok"] is True
        assert report["base_image"]["sha256_declared"] is None

    def test_a_pinned_digest_is_checked(self, base_image: Path, tmp_path: Path) -> None:
        from palette.cli import build_doctor_report
        from palette.vm.images import sha256_file

        digest = sha256_file(base_image)
        config = fake_config(base_image, tmp_path)
        profile = config.profile("default")
        config.profiles["default"] = profile.model_copy(
            update={"images": profile.images.model_copy(update={"sha256": digest})}
        )
        report = build_doctor_report(config)
        assert report["base_image"]["sha256"] == digest
        assert report["ok"] is True

    def test_a_wrong_pinned_digest_is_a_failure(self, base_image: Path, tmp_path: Path) -> None:
        from palette.cli import build_doctor_report

        config = fake_config(base_image, tmp_path)
        profile = config.profile("default")
        config.profiles["default"] = profile.model_copy(
            update={"images": profile.images.model_copy(update={"sha256": "0" * 64})}
        )
        report = build_doctor_report(config)
        assert report["ok"] is False
        assert "sha256 mismatch" in report["base_image"]["reason"]

    def test_the_version_pin_is_enforced(self, base_image: Path, tmp_path: Path) -> None:
        from palette.cli import build_doctor_report

        config = fake_config(base_image, tmp_path)
        profile = config.profile("default")
        config.profiles["default"] = profile.model_copy(
            update={"qemu": profile.qemu.model_copy(update={"version_pin": "9.9"})}
        )
        report = build_doctor_report(config)
        assert report["ok"] is False
        pinned = next(check for check in report["checks"] if check["name"] == "qemu_version_pin")
        assert pinned["ok"] is False


class TestAgainstThisHost:
    def test_a_host_without_qemu_is_reported_as_not_ready(self, tmp_path: Path) -> None:
        """This machine has no QEMU installed. The report must say so, and must not offer
        a degraded mode as an acceptable substitute."""
        from palette.cli import build_doctor_report

        profile = make_profile(tmp_path / "base.qcow2")
        profile = profile.model_copy(
            update={
                "qemu": profile.qemu.model_copy(
                    update={"binary": "qemu-system-x86_64-does-not-exist"}
                )
            }
        )
        config = PaletteConfig(
            profiles={"default": profile},
            config_root=tmp_path,
        ).model_copy(
            update={
                "host": HostConfig(
                    runtime_root=str(tmp_path / "runtime"),
                    default_profile="default",
                    qemu_search_paths=[],
                )
            }
        )
        report = build_doctor_report(config)
        assert report["ok"] is False
        missing = next(check for check in report["checks"] if check["name"] == "qemu_binary")
        assert missing["ok"] is False
        assert missing["blocking"] is True

    def test_a_profile_selecting_tcg_is_flagged_but_not_blocking(
        self, base_image: Path, tmp_path: Path
    ) -> None:
        from palette.cli import build_doctor_report

        config = fake_config(base_image, tmp_path)
        profile = config.profile("default")
        config.profiles["default"] = profile.model_copy(
            update={"qemu": profile.qemu.model_copy(update={"accelerator": "tcg"})}
        )
        report = build_doctor_report(config)
        tcg = next(check for check in report["checks"] if check["name"] == "explicit_acceleration")
        assert tcg["ok"] is False
        assert tcg["blocking"] is False


class TestCommandLine:
    def _write_repo(self, tmp_path: Path, *, profile_body: str) -> Path:
        config_root = tmp_path / "repo"
        (config_root / "profiles").mkdir(parents=True)
        (config_root / "profiles" / "default.toml").write_text(profile_body, encoding="utf-8")
        (config_root / "palette.toml").write_text(
            "[host]\n"
            f"runtime_root = '{(tmp_path / 'runtime').as_posix()}'\n"
            "qemu_search_paths = []\n",
            encoding="utf-8",
        )
        return config_root

    def test_doctor_returns_a_non_zero_exit_code_when_not_ready(
        self, tmp_path: Path, capsys
    ) -> None:
        config_root = self._write_repo(
            tmp_path,
            profile_body=(
                'profile_id = "default"\n'
                "[qemu]\n"
                "binary = 'qemu-system-x86_64-does-not-exist'\n"
                "[images]\n"
                f"base = '{(tmp_path / 'base.qcow2').as_posix()}'\n"
            ),
        )
        exit_code = main(["--root", str(config_root), "doctor", "--json"])
        assert exit_code != 0
        report = json.loads(capsys.readouterr().out)
        assert report["ok"] is False

    def test_doctor_json_is_machine_readable(
        self, base_image: Path, tmp_path: Path, capsys
    ) -> None:
        import sys

        config_root = self._write_repo(
            tmp_path,
            # TOML literal strings keep Windows paths readable: no escape processing.
            profile_body=(
                'profile_id = "default"\n'
                "frame_ttl_seconds = 30.0\n"
                "[qemu]\n"
                f"binary = '{sys.executable}'\n"
                f"argv_prefix = ['-I', '{FAKE_QEMU.as_posix()}']\n"
                f"qemu_img = '{sys.executable}'\n"
                f"qemu_img_prefix = ['-I', '{(FAKE_QEMU.parent / 'fake_qemu_img.py').as_posix()}']\n"
                'version_pin = "11.1"\n'
                'accelerator = "tcg"\n'
                "[images]\n"
                f"base = '{base_image.as_posix()}'\n"
            ),
        )
        exit_code = main(["--root", str(config_root), "doctor", "--json"])
        assert exit_code == 0
        report = json.loads(capsys.readouterr().out)
        assert report["ok"] is True
        # The profile in this temp repo says nothing about the enhancement channel, so it
        # gets the default posture — which includes the channel.
        assert report["profile"]["mode"] == "hybrid"
        assert report["profile"]["guest_tools_enabled"] is True
        assert report["profile"]["network"] == "restricted-lab"


class TestConfigCommand:
    def test_config_show_prints_the_resolved_configuration(
        self, base_image: Path, tmp_path: Path, capsys
    ) -> None:
        config_root = tmp_path / "repo"
        (config_root / "profiles").mkdir(parents=True)
        (config_root / "profiles" / "default.toml").write_text(
            'profile_id = "default"\n[qemu]\naccelerator = "whpx"\n[images]\n'
            f"base = '{base_image.as_posix()}'\n",
            encoding="utf-8",
        )
        exit_code = main(["--root", str(config_root), "config", "show"])
        assert exit_code == 0
        document = json.loads(capsys.readouterr().out)
        assert document["default_profile"] == "default"
        assert "default" in document["profiles"]


def test_the_probe_helper_agrees_with_the_fake_build(base_image: Path) -> None:
    import sys

    profile = make_profile(base_image)
    probe = probe_binary(sys.executable, prefix=profile.qemu.argv_prefix)
    assert probe.version == "11.1.2"
