"""Command-line construction and process starting."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from palette.config import FirmwareConfig, HostConfig, NetworkConfig, QemuConfig
from palette.errors import ErrorCode, PaletteError
from palette.vm.launcher import (
    ENV_WHITELIST,
    build_child_env,
    build_launch_plan,
    probe_binary,
    require_accelerator,
    run_tool,
)
from support.profiles import FAKE_QEMU, FAKE_QEMU_IMG, make_profile


def plan_for(profile, *, overlay: Path, workdir: Path, **kwargs):
    return build_launch_plan(
        profile=profile,
        binary=sys.executable,
        instance_id="inst_1",
        vm_id="desktop",
        overlay_path=overlay,
        workdir=workdir,
        **kwargs,
    )


class TestArgumentVector:
    def test_no_shell_is_involved(self, profile, tmp_path: Path) -> None:
        plan = plan_for(profile, overlay=tmp_path / "o.qcow2", workdir=tmp_path)
        assert isinstance(plan.argv, list)
        assert all(isinstance(argument, str) for argument in plan.argv)
        # Every QEMU argument is a discrete list element: nothing is ever re-quoted or
        # passed through a shell, so metacharacters have no meaning to anything.
        assert not any(argument.startswith('"') or argument.endswith('"') for argument in plan.argv)

    def test_a_path_with_spaces_survives_as_one_argument(self, profile, tmp_path: Path) -> None:
        awkward = tmp_path / "a dir with spaces & symbols"
        awkward.mkdir()
        overlay = awkward / "overlay.qcow2"
        plan = plan_for(profile, overlay=overlay, workdir=awkward)
        blocks = [
            plan.argv[i + 1] for i, argument in enumerate(plan.argv) if argument == "-blockdev"
        ]
        assert json.loads(blocks[0])["filename"] == str(overlay)
        assert plan.cwd == awkward
        # The path is never split: it lives inside a single argument, unquoted and intact.
        assert any("a dir with spaces & symbols" in argument for argument in plan.argv)

    def test_the_display_device_precedes_the_input_devices(self, profile, tmp_path: Path) -> None:
        """The plan requires this order: an input device bound to a display that does not
        exist yet falls back to an unbound handler."""
        plan = plan_for(profile, overlay=tmp_path / "o.qcow2", workdir=tmp_path)
        display = next(
            i for i, argument in enumerate(plan.argv) if argument.startswith("VGA,id=video0")
        )
        xhci = next(i for i, argument in enumerate(plan.argv) if argument.startswith("qemu-xhci"))
        keyboard = next(i for i, argument in enumerate(plan.argv) if argument.startswith("usb-kbd"))
        tablet = next(
            i for i, argument in enumerate(plan.argv) if argument.startswith("usb-tablet")
        )
        assert display < xhci < keyboard and xhci < tablet

    def test_input_devices_are_bound_to_the_display(self, profile, tmp_path: Path) -> None:
        plan = plan_for(profile, overlay=tmp_path / "o.qcow2", workdir=tmp_path)
        keyboard = next(argument for argument in plan.argv if argument.startswith("usb-kbd"))
        tablet = next(argument for argument in plan.argv if argument.startswith("usb-tablet"))
        assert "display=video0" in keyboard
        assert "display=video0" in tablet
        assert "head=0" in tablet

    def test_no_window_and_no_network(self, profile, tmp_path: Path) -> None:
        plan = plan_for(profile, overlay=tmp_path / "o.qcow2", workdir=tmp_path)
        assert plan.argv[plan.argv.index("-display") + 1] == "none"
        assert plan.argv[plan.argv.index("-nic") + 1] == "none"
        assert "-nodefaults" in plan.argv
        # No second, unauthenticated input channel.
        assert not any("vnc" in argument.lower() for argument in plan.argv)

    def test_qmp_is_on_the_child_own_stdio(self, profile, tmp_path: Path) -> None:
        plan = plan_for(profile, overlay=tmp_path / "o.qcow2", workdir=tmp_path)
        assert "stdio,id=qmp,signal=off" in plan.argv
        assert plan.argv[plan.argv.index("-mon") + 1] == "chardev=qmp,mode=control"
        assert not any(argument.startswith("tcp:") for argument in plan.argv)

    def test_the_block_graph_states_both_formats_explicitly(self, profile, tmp_path: Path) -> None:
        overlay = tmp_path / "o.qcow2"
        plan = plan_for(profile, overlay=overlay, workdir=tmp_path)
        blocks = [
            plan.argv[i + 1] for i, argument in enumerate(plan.argv) if argument == "-blockdev"
        ]
        assert len(blocks) == 2
        file_node, format_node = json.loads(blocks[0]), json.loads(blocks[1])
        assert file_node["driver"] == "file"
        assert file_node["filename"] == str(overlay)
        # The format is declared, never probed from an extension.
        assert format_node["driver"] == "qcow2"
        assert format_node["file"] == "disk-file"
        assert " " not in blocks[0] and " " not in blocks[1]

    def test_the_guest_starts_paused(self, profile, tmp_path: Path) -> None:
        plan = plan_for(profile, overlay=tmp_path / "o.qcow2", workdir=tmp_path)
        assert "-S" in plan.argv
        assert any("paused" in note for note in plan.notes)

    def test_the_machine_and_resource_limits_come_from_the_profile(
        self, profile, tmp_path: Path
    ) -> None:
        plan = plan_for(profile, overlay=tmp_path / "o.qcow2", workdir=tmp_path)
        assert plan.argv[plan.argv.index("-machine") + 1] == "q35"
        assert plan.argv[plan.argv.index("-m") + 1] == "512"
        assert plan.argv[plan.argv.index("-smp") + 1] == "1"

    def test_firmware_variables_are_per_instance(self, profile, tmp_path: Path) -> None:
        configured = profile.model_copy(
            update={
                "qemu": profile.qemu.model_copy(
                    update={
                        "firmware": FirmwareConfig(
                            code=str(tmp_path / "code.fd"), vars=str(tmp_path / "vars.fd")
                        )
                    }
                )
            }
        )
        vars_path = tmp_path / "instance-vars.fd"
        plan = plan_for(
            configured,
            overlay=tmp_path / "o.qcow2",
            workdir=tmp_path,
            hardware_vars_path=vars_path,
        )
        drives = [plan.argv[i + 1] for i, argument in enumerate(plan.argv) if argument == "-drive"]
        assert f"file={tmp_path / 'code.fd'}" in drives[0]
        assert "readonly=on" in drives[0]
        assert drives[1].endswith(f"file={vars_path}")
        assert "readonly" not in drives[1]

    def test_a_networked_profile_notes_that_egress_must_be_audited(
        self, profile, tmp_path: Path
    ) -> None:
        configured = profile.model_copy(
            update={"network": NetworkConfig(mode="restricted-lab", restrict=True)}
        )
        plan = plan_for(configured, overlay=tmp_path / "o.qcow2", workdir=tmp_path)
        assert plan.argv[plan.argv.index("-nic") + 1] == "user,restrict=on"
        assert any("audited" in note for note in plan.notes)


class TestEnvironment:
    def test_only_whitelisted_variables_are_inherited(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """QEMU must not inherit the operator's tokens just because they were exported."""
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "leak-me")
        monkeypatch.setenv("PALETTE_TEST_SECRET", "also-leak-me")
        monkeypatch.setenv("SYSTEMROOT", "C:\\Windows")
        env = build_child_env()
        assert "AWS_SECRET_ACCESS_KEY" not in env
        assert "PALETTE_TEST_SECRET" not in env
        assert set(env) <= set(ENV_WHITELIST)

    def test_the_plan_carries_the_whitelisted_environment(self, profile, tmp_path: Path) -> None:
        plan = plan_for(profile, overlay=tmp_path / "o.qcow2", workdir=tmp_path)
        assert set(plan.env) <= set(ENV_WHITELIST)


class TestAccelerator:
    def test_a_requested_accelerator_the_build_lacks_is_refused(self, profile) -> None:
        probe = probe_binary(sys.executable)  # not QEMU, so no accelerator list
        probe.accelerators = ["tcg"]
        with pytest.raises(PaletteError) as info:
            require_accelerator(profile, probe)
        assert info.value.code is ErrorCode.ACCELERATION_UNAVAILABLE
        assert info.value.details["available"] == ["tcg"]

    def test_a_requested_accelerator_the_build_offers_is_accepted(self, profile) -> None:
        probe = probe_binary(sys.executable)
        probe.accelerators = ["whpx", "tcg"]
        assert require_accelerator(profile, probe) == "whpx"

    def test_tcg_is_usable_but_flagged(self, profile, caplog: pytest.LogCaptureFixture) -> None:
        trusted = profile.model_copy(
            update={"qemu": profile.qemu.model_copy(update={"accelerator": "tcg"})}
        )
        probe = probe_binary(sys.executable)
        probe.accelerators = ["tcg"]
        with caplog.at_level("WARNING"):
            assert require_accelerator(trusted, probe) == "tcg"
        assert any("no guest isolation guarantee" in record.message for record in caplog.records)

    def test_an_unknown_accelerator_list_is_not_treated_as_a_failure(self, profile) -> None:
        """A build that does not answer ``-accel help`` gets the benefit of the doubt at
        this layer; the start itself is the real proof."""
        probe = probe_binary(sys.executable)
        probe.accelerators = []
        assert require_accelerator(profile, probe) == "whpx"


class TestProbe:
    def test_the_fake_build_is_characterised_correctly(self, profile, base_image: Path) -> None:
        probe = probe_binary(sys.executable, with_devices=True, prefix=profile.qemu.argv_prefix)
        assert probe.found is True
        assert probe.version == "11.1.2"
        assert "whpx" in probe.accelerators
        assert probe.supports_device("usb-tablet")
        assert probe.supports_device("qemu-xhci")

    def test_a_binary_that_cannot_be_started_is_reported(self) -> None:
        probe = probe_binary("definitely-not-a-real-binary-xyz")
        assert probe.found is False
        assert probe.error

    def test_the_prefix_is_used_for_probing_too(self, base_image: Path) -> None:
        """Probing the bare interpreter would interrogate the wrapper, not QEMU."""
        profile = make_profile(base_image)
        without_prefix = probe_binary(sys.executable)
        with_prefix = probe_binary(sys.executable, prefix=profile.qemu.argv_prefix)
        assert without_prefix.version != "11.1.2"
        assert with_prefix.version == "11.1.2"


class TestHelperExecution:
    async def test_a_missing_helper_is_reported_as_missing(self) -> None:
        with pytest.raises(PaletteError) as info:
            await run_tool(["definitely-not-a-real-binary-xyz", "--version"])
        assert info.value.code is ErrorCode.QEMU_NOT_FOUND

    async def test_a_helper_is_run_with_a_fixed_argument_vector(self, base_image: Path) -> None:
        profile = make_profile(base_image)
        code, stdout, _stderr = await run_tool(
            [sys.executable, *profile.qemu.qemu_img_prefix, "--version"]
        )
        assert code == 0
        assert "qemu-img" in stdout

    async def test_the_fake_qemu_img_is_found_through_the_profile(self, profile) -> None:
        assert profile.qemu.qemu_img == sys.executable
        assert FAKE_QEMU_IMG.is_file()
        assert FAKE_QEMU.is_file()


class TestProfileGuardrails:
    def test_a_profile_cannot_smuggle_a_monitor_through_extra_args(self) -> None:
        with pytest.raises(ValidationError) as info:
            QemuConfig(extra_args=["-mon vc,mode=control"])
        assert "monitor or display" in str(info.value)

    def test_an_accelerator_fallback_cannot_be_enabled(self) -> None:
        with pytest.raises(ValidationError) as info:
            QemuConfig(allow_accelerator_fallback=True)
        assert "fallback" in str(info.value)

    def test_the_search_paths_default_is_not_empty(self) -> None:
        assert HostConfig().qemu_search_paths
