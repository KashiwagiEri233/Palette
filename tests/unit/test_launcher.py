"""Command-line construction, path safety, environment filtering and accelerator checks."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from palette.errors import ErrorCode, PaletteError
from palette.vm import launcher
from palette.vm.launcher import (
    ENV_WHITELIST,
    ExtraVolume,
    build_child_env,
    build_launch_plan,
    probe_binary,
    require_accelerator,
)


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
    def test_steady_vnc_does_not_require_a_helper_or_posix_fd_passing(
        self, profile, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setattr(launcher, "IS_WINDOWS", False)
        plan = plan_for(
            profile,
            overlay=tmp_path / "o.qcow2",
            workdir=tmp_path,
            steady_display=True,
            managed_console=True,
        )
        assert plan.steady_display and not plan.experimental_display
        assert "dbus,p2p=yes,gl=off" in plan.argv and "-vnc" in plan.argv
        assert "stdio,id=qmp,signal=off" in plan.argv
        assert plan.qmp_endpoint is None

    @pytest.mark.parametrize("windows", [True, False])
    def test_dbus_opt_in_keeps_one_private_qmp_and_vnc_fallback(
        self, profile, tmp_path, monkeypatch, windows
    ) -> None:
        from palette import windows_unix

        monkeypatch.setattr(launcher, "IS_WINDOWS", windows)
        monkeypatch.setattr(windows_unix, "endpoint_directory", lambda: tmp_path)
        monkeypatch.setattr(windows_unix, "validate_endpoint", lambda value: value.encode())
        plan = plan_for(
            profile,
            overlay=tmp_path / "o.qcow2",
            workdir=tmp_path,
            experimental_display=True,
            managed_console=True,
        )
        assert "dbus,p2p=yes,gl=off" in plan.argv
        assert plan.argv.count("-mon") == 1
        assert plan.qmp_endpoint == str(tmp_path / "qmp.sock")
        assert "stdio,id=qmp,signal=off" not in plan.argv
        assert "-vnc" in plan.argv

    def test_windows_enhancements_use_private_nonblocking_sockets(
        self, profile, tmp_path, monkeypatch
    ) -> None:
        from palette import windows_unix

        monkeypatch.setattr(launcher, "IS_WINDOWS", True)
        monkeypatch.setattr(windows_unix, "endpoint_directory", lambda: tmp_path)
        monkeypatch.setattr(windows_unix, "validate_endpoint", lambda value: value.encode())
        profile = profile.model_copy(
            update={
                "guest_agent": profile.guest_agent.model_copy(update={"enabled": True}),
                "guest_tools": profile.guest_tools.model_copy(update={"enabled": True}),
                "guest_desktop": profile.guest_desktop.model_copy(
                    update={"resize": True, "shared_folders": True}
                ),
            }
        )
        plan = plan_for(profile, overlay=tmp_path / "o.qcow2", workdir=tmp_path)
        endpoints = [
            plan.guest_agent_endpoint,
            plan.guest_tools_endpoint,
            plan.webdav_endpoint,
            plan.session_endpoint,
        ]
        assert all(Path(endpoint).parent == tmp_path for endpoint in endpoints)
        assert plan.guest_ipc_directory == tmp_path
        backends = [arg for arg in plan.argv if arg.startswith("socket,id=palette-")]
        assert len(backends) == 4
        assert all("server=on,wait=off" in arg for arg in backends)
        assert not any(arg.startswith("pipe,") for arg in plan.argv)
        assert plan.qmp_endpoint == str(tmp_path / "qmp.sock")
        assert any(arg.startswith("socket,id=qmp,") for arg in plan.argv)

    def test_managed_console_uses_an_os_selected_port(self, profile, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(launcher, "_available_console_port", lambda: 45106)
        plan = plan_for(
            profile, overlay=tmp_path / "o.qcow2", workdir=tmp_path, managed_console=True
        )
        assert plan.argv[plan.argv.index("-vnc") + 1] == (
            "127.0.0.1:39206,to=39305,password=on,share=force-shared"
        )

    @pytest.mark.parametrize("name", ["安装\n[device].iso", '安装".iso', "安装😀.iso"])
    def test_unrepresentable_or_unsafe_config_paths_fail_before_spawn(
        self, profile, tmp_path: Path, name: str
    ) -> None:
        with pytest.raises(PaletteError) as failure:
            plan_for(
                profile,
                overlay=tmp_path / "o.qcow2",
                workdir=tmp_path,
                extra_volumes=(ExtraVolume(path="E:/" + name, media="cdrom"),),
            )
        assert failure.value.code == ErrorCode.STARTUP_FAILED


class TestExtraChannelsAndEnvironment:
    @pytest.mark.parametrize(
        "arguments",
        [["-mon", "vc"], ["-vnc", ":0"], ["-chardev", "socket,id=x,port=4444"]],
    )
    def test_a_second_channel_is_flagged(
        self, profile, tmp_path: Path, arguments: list[str]
    ) -> None:
        plan = plan_for(
            profile, overlay=tmp_path / "o.qcow2", workdir=tmp_path, caller_args=arguments
        )
        assert any("single-writer guarantee no longer holds" in note for note in plan.notes)

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


class TestAccelerator:
    def test_a_requested_accelerator_the_build_lacks_is_refused(self, profile) -> None:
        probe = probe_binary(sys.executable)  # not QEMU, so no accelerator list
        probe.accelerators = ["whpx"]
        with pytest.raises(PaletteError) as info:
            require_accelerator(profile, probe)
        assert info.value.code is ErrorCode.ACCELERATION_UNAVAILABLE
        assert info.value.details["available"] == ["whpx"]
