"""Authorization and path confinement."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from palette.config import (
    GuestToolsConfig,
    ImagesConfig,
    LifecyclePolicy,
    NetworkConfig,
    Profile,
    QemuConfig,
)
from palette.errors import ErrorCode, PaletteError
from palette.policy import PathGuard, Policy, validate_identifier


def make_policy(guard: PathGuard, **overrides) -> Policy:
    data = {
        "profile_id": "test",
        "qemu": QemuConfig(),
        "images": ImagesConfig(base=str(Path(os.getcwd()) / "missing.qcow2")),
        "network": NetworkConfig(mode="offline", restrict=True),
    }
    data.update(overrides)
    return Policy(Profile(**data), guard)  # type: ignore[arg-type]


class TestIdentifiers:
    @pytest.mark.parametrize("value", ["desktop", "vm-1", "a.b_c", "A" * 64])
    def test_reasonable_identifiers_pass(self, value: str) -> None:
        assert validate_identifier(value) == value

    @pytest.mark.parametrize(
        "value",
        [
            "",
            ".",
            "..",
            "../escape",
            "a/b",
            "a\\b",
            "with space",
            "a" * 65,
            "-leading",
            "c:drive",
            "\x00",
        ],
    )
    def test_dangerous_identifiers_are_refused(self, value: str) -> None:
        with pytest.raises(PaletteError) as info:
            validate_identifier(value)
        assert info.value.code is ErrorCode.INVALID_ARGUMENT


class TestPathGuard:
    def test_paths_inside_the_root_resolve(self, guard: PathGuard, runtime_root: Path) -> None:
        assert (
            guard.resolve("vms", "desktop", "inst_1") == runtime_root / "vms" / "desktop" / "inst_1"
        )

    def test_traversal_out_of_the_root_is_refused(self, guard: PathGuard) -> None:
        with pytest.raises(PaletteError) as info:
            guard.resolve("..", "elsewhere")
        assert info.value.code is ErrorCode.PATH_DENIED

    def test_deep_traversal_is_refused(self, guard: PathGuard) -> None:
        with pytest.raises(PaletteError):
            guard.resolve("vms", "..", "..", "..", "Windows")

    @pytest.mark.parametrize("value", [r"C:\Windows\System32", "/etc", "\\\\server\\share"])
    def test_absolute_components_are_refused(self, guard: PathGuard, value: str) -> None:
        with pytest.raises(PaletteError) as info:
            guard.resolve(value)
        assert info.value.code is ErrorCode.PATH_DENIED

    def test_contains_agrees_with_resolve(self, guard: PathGuard, runtime_root: Path) -> None:
        assert guard.contains(runtime_root / "vms" / "desktop")
        assert not guard.contains(runtime_root.parent / "elsewhere")

    def test_assert_managed_refuses_an_unmanaged_path(
        self, guard: PathGuard, tmp_path: Path
    ) -> None:
        with pytest.raises(PaletteError) as info:
            guard.assert_managed(tmp_path / "unmanaged")
        assert info.value.code is ErrorCode.PATH_DENIED

    def test_must_exist_reports_a_missing_managed_file(self, guard: PathGuard) -> None:
        with pytest.raises(PaletteError) as info:
            guard.resolve("vms", "desktop", "nothing.json", must_exist=True)
        assert info.value.code is ErrorCode.IMAGE_MISSING

    @pytest.mark.skipif(sys.platform != "win32", reason="reparse points are a Windows concept")
    def test_a_junction_inside_the_root_is_refused(self, guard: PathGuard, tmp_path: Path) -> None:
        """A junction planted inside the runtime root would otherwise redirect cleanup
        outside it, which is exactly what the plan requires checking for."""
        import shutil
        import subprocess

        cmd = shutil.which("cmd")
        if cmd is None:  # pragma: no cover - Windows always has cmd
            pytest.skip("cmd.exe is not on PATH")
        outside = tmp_path / "outside"
        outside.mkdir()
        link = guard.root / "link"
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [cmd, "/c", "mklink", "/J", str(link), str(outside)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:  # pragma: no cover - environment cannot create junctions
            pytest.skip("could not create a junction in this environment")
        with pytest.raises(PaletteError) as info:
            guard.resolve("link", "payload")
        assert info.value.code is ErrorCode.PATH_DENIED


class TestLifecyclePermissions:
    def test_agents_are_refused_by_default(self, guard: PathGuard) -> None:
        policy = make_policy(guard)
        for operation in ("start", "stop", "force_stop", "reset"):
            with pytest.raises(PaletteError) as info:
                policy.require_lifecycle(operation, actor="agent")  # type: ignore[arg-type]
            assert info.value.code is ErrorCode.POLICY_DENIED

    def test_operators_are_allowed(self, guard: PathGuard) -> None:
        policy = make_policy(guard)
        for operation in ("start", "stop", "force_stop", "reset"):
            policy.require_lifecycle(operation, actor="operator")  # type: ignore[arg-type]

    def test_a_granted_operation_is_allowed_for_agents(self, guard: PathGuard) -> None:
        policy = make_policy(
            guard,
            lifecycle=LifecyclePolicy(allow_agent_stop=True),
        )
        policy.require_lifecycle("stop", actor="agent")
        with pytest.raises(PaletteError):
            policy.require_lifecycle("reset", actor="agent")

    def test_capabilities_reflect_the_grants(self, guard: PathGuard) -> None:
        policy = make_policy(guard)
        assert policy.capabilities() == ["observe", "control"]
        granted = make_policy(guard, lifecycle=LifecyclePolicy(allow_agent_reset=True))
        assert "lifecycle.reset" in granted.capabilities()


class TestCapabilityGating:
    def test_guest_tools_are_unavailable_when_disabled(self, guard: PathGuard) -> None:
        policy = make_policy(guard)
        with pytest.raises(PaletteError) as info:
            policy.require_capability("guest.input_text")
        assert info.value.code is ErrorCode.GUEST_TOOL_UNAVAILABLE

    def test_an_unlisted_capability_is_refused_even_when_enabled(self, guard: PathGuard) -> None:
        policy = make_policy(
            guard,
            guest_tools=GuestToolsConfig(enabled=True, capabilities=["guest.input_text"]),
        )
        policy.require_capability("guest.input_text")
        with pytest.raises(PaletteError) as info:
            policy.require_capability("guest.launch_app")
        assert info.value.code is ErrorCode.UNSUPPORTED_CAPABILITY

    def test_unknown_capabilities_are_refused(self, guard: PathGuard) -> None:
        with pytest.raises(PaletteError) as info:
            make_policy(guard).require_capability("guest.shell")
        assert info.value.code is ErrorCode.UNSUPPORTED_CAPABILITY


class TestAppRegistry:
    def test_unregistered_apps_are_refused(self, guard: PathGuard) -> None:
        policy = make_policy(
            guard,
            guest_tools=GuestToolsConfig(
                enabled=True,
                capabilities=["guest.launch_app"],
                allowed_apps={"text-editor": {"executable": "/usr/bin/mousepad"}},
            ),
        )
        assert policy.require_app("text-editor")["executable"] == "/usr/bin/mousepad"
        with pytest.raises(PaletteError) as info:
            policy.require_app("bash")
        assert info.value.code is ErrorCode.APP_NOT_ALLOWED


class TestAssetsAndNetwork:
    def test_a_missing_base_image_is_reported(self, guard: PathGuard) -> None:
        with pytest.raises(PaletteError) as info:
            make_policy(guard).require_base_image()
        assert info.value.code is ErrorCode.IMAGE_MISSING

    def test_a_relative_base_image_path_is_invalid(self, guard: PathGuard) -> None:
        policy = make_policy(guard, images=ImagesConfig(base="images/base.qcow2"))
        with pytest.raises(PaletteError) as info:
            policy.require_base_image()
        assert info.value.code is ErrorCode.IMAGE_INVALID

    def test_a_well_formed_base_image_is_accepted(self, guard: PathGuard, base_image: Path) -> None:
        policy = make_policy(guard, images=ImagesConfig(base=str(base_image), format="qcow2"))
        assert policy.require_base_image() == base_image

    def test_a_mismatched_extension_is_rejected(self, guard: PathGuard, tmp_path: Path) -> None:
        wrong = tmp_path / "base.raw"
        wrong.write_bytes(b"x")
        policy = make_policy(guard, images=ImagesConfig(base=str(wrong), format="qcow2"))
        with pytest.raises(PaletteError) as info:
            policy.require_base_image()
        assert info.value.code is ErrorCode.IMAGE_INVALID

    def test_only_the_offline_mode_is_accepted_by_this_build(self, guard: PathGuard) -> None:
        policy = make_policy(guard, network=NetworkConfig(mode="restricted-lab", restrict=True))
        with pytest.raises(PaletteError) as info:
            policy.require_network_offline()
        assert info.value.code is ErrorCode.POLICY_DENIED
