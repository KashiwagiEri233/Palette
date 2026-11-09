"""Command-line construction and process starting."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from palette.config import FirmwareConfig, NetworkConfig
from palette.errors import ErrorCode, PaletteError
from palette.vm import launcher
from palette.vm.launcher import (
    ENV_WHITELIST,
    ExtraVolume,
    build_child_env,
    build_launch_plan,
    probe_binary,
    require_accelerator,
    run_tool,
)
from support.profiles import make_profile


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
    ):
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
    ):
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
    ):
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

    def test_managed_console_uses_an_os_selected_port(self, profile, tmp_path, monkeypatch):
        monkeypatch.setattr(launcher, "_available_console_port", lambda: 45106)
        plan = plan_for(
            profile, overlay=tmp_path / "o.qcow2", workdir=tmp_path, managed_console=True
        )
        assert plan.argv[plan.argv.index("-vnc") + 1] == (
            "127.0.0.1:39206,to=39305,password=on,share=force-shared"
        )

    def test_console_port_range_cannot_overflow_tcp(self, profile, tmp_path, monkeypatch):
        monkeypatch.setattr(launcher, "_available_console_port", lambda: 65500)
        plan = plan_for(
            profile,
            overlay=tmp_path / "o.qcow2",
            workdir=tmp_path,
            managed_console=True,
        )
        assert plan.argv[plan.argv.index("-vnc") + 1] == (
            "127.0.0.1:59600,to=59635,password=on,share=force-shared"
        )

    @pytest.mark.parametrize("port", [5899, 65536])
    def test_invalid_console_port_is_rejected(self, profile, tmp_path, port, monkeypatch):
        monkeypatch.setattr(launcher, "_available_console_port", lambda: port)
        with pytest.raises(PaletteError, match="invalid local display port"):
            plan_for(
                profile,
                overlay=tmp_path / "o.qcow2",
                workdir=tmp_path,
                managed_console=True,
            )

    def test_headless_plan_never_allocates_a_console_port(self, profile, tmp_path, monkeypatch):
        def unexpected():
            pytest.fail("headless QEMU should not allocate a VNC port")

        monkeypatch.setattr(launcher, "_available_console_port", unexpected)
        plan_for(profile, overlay=tmp_path / "o.qcow2", workdir=tmp_path)

    def test_console_probe_uses_ipv4_loopback_and_releases_socket(self, monkeypatch):
        class Probe:
            address = None
            closed = False

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                self.closed = True

            def bind(self, address):
                self.address = address

            def getsockname(self):
                return ("127.0.0.1", 45106)

        probe = Probe()
        monkeypatch.setattr(launcher.socket, "socket", lambda *_args: probe)
        assert launcher._available_console_port() == 45106
        assert probe.address == ("127.0.0.1", 0)
        assert probe.closed

    def test_console_probe_reports_loopback_permission_failure(self, monkeypatch):
        def unavailable(*_args):
            raise OSError(10013, "access denied")

        monkeypatch.setattr(launcher.socket, "socket", unavailable)
        with pytest.raises(PaletteError, match="check loopback socket permissions") as failure:
            launcher._available_console_port()
        assert failure.value.code == ErrorCode.STARTUP_FAILED

    def test_console_probe_recovers_from_an_unusually_low_ephemeral_port(self, monkeypatch):
        bound = []

        class Probe:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return None

            def bind(self, address):
                bound.append(address)

            def getsockname(self):
                return ("127.0.0.1", 4200)

        monkeypatch.setattr(launcher.socket, "socket", lambda *_args: Probe())
        monkeypatch.setattr(launcher.secrets, "randbelow", lambda _max: 123)
        assert launcher._available_console_port() == 49275
        assert bound == [("127.0.0.1", 0), ("127.0.0.1", 49275)]

    def test_tools_optical_drive_is_separate_read_only_and_has_no_boot_priority(
        self, profile, tmp_path
    ):
        tools = tmp_path / "tools, media.iso"
        installer = tmp_path / "windows.iso"
        plan = plan_for(
            profile,
            overlay=tmp_path / "o.qcow2",
            workdir=tmp_path,
            tools_iso=tools,
            extra_volumes=[ExtraVolume(path=str(installer), media="cdrom", boot_index=0)],
        )
        drive = plan.argv[plan.argv.index("-drive") + 1]
        assert "id=palette-tools" in drive and "readonly=on" in drive
        assert "tools,, media.iso" in drive
        assert "usb-bot,id=palette-tools-usb,bus=xhci.0" in plan.argv
        device = next(arg for arg in plan.argv if arg.startswith("scsi-cd,id=palette-tools-cd,"))
        assert "bus=palette-tools-usb.0" in device and "bootindex" not in device
        assert any(arg.startswith("scsi-cd,") and "bootindex=0" in arg for arg in plan.argv)

    def test_tools_drive_is_not_added_until_media_is_selected(self, profile, tmp_path):
        plan = plan_for(profile, overlay=tmp_path / "o.qcow2", workdir=tmp_path)
        assert not any("palette-tools" in arg for arg in plan.argv)

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
        assert plan.argv[plan.argv.index("-display") + 1] == "none"
        assert plan.argv[plan.argv.index("-nic") + 1] == "none"
        assert "-S" in plan.argv
        assert plan.argv[plan.argv.index("-smp") + 1] == str(profile.resources.vcpus)
        assert plan.argv[plan.argv.index("-m") + 1] == str(profile.resources.memory_mib)

    def test_input_devices_are_bound_to_the_display(self, profile, tmp_path: Path) -> None:
        plan = plan_for(profile, overlay=tmp_path / "o.qcow2", workdir=tmp_path)
        keyboard = next(argument for argument in plan.argv if argument.startswith("usb-kbd"))
        tablet = next(argument for argument in plan.argv if argument.startswith("usb-tablet"))
        assert "display=video0" in keyboard
        assert "display=video0" in tablet
        assert "head=0" in tablet

    def test_qmp_is_on_a_private_instance_transport(self, profile, tmp_path: Path) -> None:
        plan = plan_for(profile, overlay=tmp_path / "o.qcow2", workdir=tmp_path)
        if launcher.IS_WINDOWS:
            assert plan.qmp_endpoint
            assert any(arg.startswith("socket,id=qmp,") for arg in plan.argv)
        else:
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

    def test_the_network_mode_decides_the_nic_argument(self, profile, tmp_path: Path) -> None:
        def nic(mode: str) -> str:
            configured = profile.model_copy(update={"network": NetworkConfig(mode=mode)})  # type: ignore[arg-type]
            plan = plan_for(configured, overlay=tmp_path / "o.qcow2", workdir=tmp_path)
            return plan.argv[plan.argv.index("-nic") + 1]

        assert nic("offline") == "none"
        assert nic("restricted-lab") == "user,restrict=on"
        # `nat` is user-mode networking without the sandbox: the guest gets the host's reach.
        assert nic("nat") == "user"

    def test_port_forwards_and_a_fixed_mac_are_passed_through(
        self, profile, tmp_path: Path
    ) -> None:
        configured = profile.model_copy(
            update={
                "network": NetworkConfig(
                    mode="nat", mac="52:54:00:12:34:56", hostfwd=["tcp::2222-:22"]
                )
            }
        )
        plan = plan_for(configured, overlay=tmp_path / "o.qcow2", workdir=tmp_path)
        nic = plan.argv[plan.argv.index("-nic") + 1]
        assert "mac=52:54:00:12:34:56" in nic
        assert "hostfwd=tcp::2222-:22" in nic
        # A forward deliberately punches through the isolation the other modes provide, so it
        # is called out rather than passing unnoticed.
        assert any("port forward" in note for note in plan.notes)

    def test_nat_is_described_as_having_no_egress_policy(self, profile, tmp_path: Path) -> None:
        configured = profile.model_copy(update={"network": NetworkConfig(mode="nat")})
        plan = plan_for(configured, overlay=tmp_path / "o.qcow2", workdir=tmp_path)
        assert any("no egress policy" in note for note in plan.notes)


class TestExtraVolumes:
    """Caller-supplied disks and CD-ROMs."""

    def test_an_iso_gets_a_scsi_controller_and_an_optical_device(
        self, profile, tmp_path: Path
    ) -> None:
        plan = plan_for(
            profile,
            overlay=tmp_path / "o.qcow2",
            workdir=tmp_path,
            extra_volumes=(ExtraVolume(path="D:/templates/installer.iso", media="cdrom"),),
        )
        assert "virtio-scsi-pci,id=scsi0" in plan.argv
        device = next(argument for argument in plan.argv if argument.startswith("scsi-cd"))
        assert device == "scsi-cd,drive=extra0,bus=scsi0.0"
        blocks = [plan.argv[i + 1] for i, a in enumerate(plan.argv) if a == "-blockdev"]
        assert len(blocks) == 4
        nodes = [json.loads(block) for block in blocks]
        assert nodes[2]["filename"] == "D:/templates/installer.iso"
        assert nodes[2]["read-only"] is True
        assert nodes[3]["driver"] == "raw"

    def test_an_extra_disk_uses_virtio_blk_like_the_instance_disk(
        self, profile, tmp_path: Path
    ) -> None:
        plan = plan_for(
            profile,
            overlay=tmp_path / "o.qcow2",
            workdir=tmp_path,
            extra_volumes=(
                ExtraVolume(path="E:/data/scratch.qcow2", media="disk", format="qcow2"),
            ),
        )
        assert "virtio-blk-pci,drive=extra0" in plan.argv
        assert "virtio-scsi-pci,id=scsi0" not in plan.argv
        assert any("caller-supplied disk" in note for note in plan.notes)

    def test_several_volumes_get_distinct_nodes(self, profile, tmp_path: Path) -> None:
        plan = plan_for(
            profile,
            overlay=tmp_path / "o.qcow2",
            workdir=tmp_path,
            extra_volumes=(
                ExtraVolume(path="A.iso", media="cdrom"),
                ExtraVolume(path="B.iso", media="cdrom"),
            ),
        )
        assert plan.argv.count("virtio-scsi-pci,id=scsi0") == 1  # one controller, not two
        assert "scsi-cd,drive=extra0,bus=scsi0.0" in plan.argv
        assert "scsi-cd,drive=extra1,bus=scsi0.0" in plan.argv


class TestWindowsHostPaths:
    @pytest.fixture(autouse=True)
    def windows_code_page(self, monkeypatch):
        monkeypatch.setattr("palette.vm.launcher.IS_WINDOWS", True)
        monkeypatch.setattr("palette.vm.launcher.locale.getencoding", lambda: "cp936")

    def test_chinese_media_path_uses_native_config_without_option_injection(
        self, profile, tmp_path: Path
    ) -> None:
        path = "E:/迅雷下载/安装 盘,readonly=off.iso"
        plan = plan_for(
            profile,
            overlay=tmp_path / "o.qcow2",
            workdir=tmp_path,
            extra_volumes=(ExtraVolume(path=path, media="cdrom", boot_index=0),),
        )
        assert plan.host_path_config is not None
        config = plan.host_path_config.decode("cp936")
        assert f'file = "{path}"' in config
        assert 'format = "raw"' in config
        assert 'readonly = "on"' in config
        assert 'if = "none"' in config
        assert 'id = "extra0"' in config
        assert plan.argv[plan.argv.index("-readconfig") + 1] == "qemu-host-paths.conf"
        assert "scsi-cd,drive=extra0,bus=scsi0.0,bootindex=0" in plan.argv
        assert plan.argv.count("-blockdev") == 2  # ASCII overlay keeps its graph.

    def test_chinese_overlay_keeps_explicit_format_and_disk_boot_order(
        self, profile, tmp_path: Path
    ) -> None:
        plan = plan_for(profile, overlay=tmp_path / "磁盘.qcow2", workdir=tmp_path)
        assert plan.host_path_config is not None
        config = plan.host_path_config.decode("cp936")
        assert 'format = "qcow2"' in config
        assert 'id = "disk"' in config
        assert 'discard = "unmap"' in config
        assert "virtio-blk-pci,drive=disk,bootindex=0" in plan.argv
        assert "-blockdev" not in plan.argv

    def test_firmware_code_and_vars_keep_their_order_across_encodings(
        self, profile, tmp_path: Path
    ) -> None:
        configured = profile.model_copy(
            update={
                "qemu": profile.qemu.model_copy(
                    update={"firmware": FirmwareConfig(code="E:/固件/code.fd", vars="E:/vars.fd")}
                )
            }
        )
        plan = plan_for(
            configured,
            overlay=tmp_path / "o.qcow2",
            workdir=tmp_path,
            hardware_vars_path=tmp_path / "vars.fd",
        )
        assert plan.host_path_config is not None
        config = plan.host_path_config.decode("cp936")
        assert config.index("E:/固件/code.fd") < config.index(str(tmp_path / "vars.fd"))
        assert config.count("[drive]") == 2
        assert "-drive" not in plan.argv

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

    def test_posix_unicode_paths_keep_the_json_block_graph(
        self, profile, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr("palette.vm.launcher.IS_WINDOWS", False)
        overlay = tmp_path / "磁盘.qcow2"
        plan = plan_for(profile, overlay=overlay, workdir=tmp_path)
        assert plan.host_path_config is None
        assert json.loads(plan.argv[plan.argv.index("-blockdev") + 1])["filename"] == str(overlay)


class TestExtraChannels:
    """A second control or display channel breaks the single-writer assumption. It is
    allowed — a caller who may pass arguments may pass any of them — but never silently."""

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


class TestAccelerator:
    def test_a_requested_accelerator_the_build_lacks_is_refused(self, profile) -> None:
        probe = probe_binary(sys.executable)  # not QEMU, so no accelerator list
        probe.accelerators = ["whpx"]
        with pytest.raises(PaletteError) as info:
            require_accelerator(profile, probe)
        assert info.value.code is ErrorCode.ACCELERATION_UNAVAILABLE
        assert info.value.details["available"] == ["whpx"]

    def test_a_requested_accelerator_the_build_offers_is_accepted(self, profile) -> None:
        probe = probe_binary(sys.executable)
        probe.accelerators = ["whpx", "tcg"]
        assert require_accelerator(profile, probe) == "tcg"

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
        assert require_accelerator(profile, probe) == "tcg"


class TestProbe:
    def test_a_binary_that_cannot_be_started_is_reported(self) -> None:
        probe = probe_binary("definitely-not-a-real-binary-xyz")
        assert probe.found is False
        assert probe.error


class TestHelperExecution:
    async def test_a_helper_is_run_with_a_fixed_argument_vector(self, base_image: Path) -> None:
        profile = make_profile(base_image)
        code, stdout, _stderr = await run_tool(
            [sys.executable, *profile.qemu.qemu_img_prefix, "--version"]
        )
        assert code == 0
        assert "qemu-img" in stdout
