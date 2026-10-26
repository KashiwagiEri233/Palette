"""Trusted configuration: loading, validation and the controls that may not be relaxed."""

from __future__ import annotations

from pathlib import Path

import pytest

from palette.config import find_config_root, load_config
from palette.errors import ErrorCode, PaletteError


def profile_body(
    base: Path,
    *,
    profile_id: str = "default",
    qemu: str = "",
    lifecycle: str = "",
    guest_tools: str = "",
    network: str = "",
) -> str:
    """Build a profile file with the test's overrides folded into the right tables.

    Paths are written as TOML literal strings: a Windows path in a basic string would be
    read as escape sequences, and the test would be measuring the wrong failure.
    """
    return (
        f'profile_id = "{profile_id}"\n'
        "[qemu]\n"
        'accelerator = "whpx"\n'
        f"{qemu}"
        "[images]\n"
        f"base = '{base.as_posix()}'\n"
        "[network]\n"
        'mode = "offline"\n'
        f"{network}"
        "[lifecycle]\n"
        f"{lifecycle}"
        "[guest_tools]\n"
        f"{guest_tools}"
    )


def write_profile(root: Path, name: str, body: str) -> Path:
    profiles = root / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    path = profiles / f"{name}.toml"
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    return root


def load(repo: Path):
    return load_config(repo)


class TestLoading:
    def test_a_minimal_profile_loads_with_production_defaults(
        self, repo: Path, base_image: Path
    ) -> None:
        # Deliberately no [network], [lifecycle] or [guest_tools] tables: this asserts what
        # a profile that says nothing at all gets.
        write_profile(
            repo,
            "default",
            'profile_id = "default"\n'
            "[qemu]\n"
            'accelerator = "whpx"\n'
            "[images]\n"
            f"base = '{base_image.as_posix()}'\n",
        )
        config = load(repo)
        profile = config.profile(None)
        assert profile.profile_id == "default"
        # A profile that says nothing still gets a complete, deliberate posture rather than
        # an empty one. These are the current defaults; each is a switch.
        assert profile.network.mode == "restricted-lab"  # a NIC, sandboxed
        assert profile.network.attaches_nic is True
        assert profile.qemu.allow_accelerator_fallback is False  # still refuses to degrade
        assert profile.guest_tools.enabled is True
        assert set(profile.guest_tools.capabilities) == {
            "guest.input_text",
            "guest.launch_app",
            "guest.filesystem",
            "guest.powershell",
            "guest.process",
            "guest.registry",
            "guest.app",
            "guest.snapshot",
            "guest.batch",
            "guest.clipboard",
        }
        assert profile.lifecycle.allow_agent_start is True
        assert profile.lifecycle.allow_agent_images is True
        assert profile.lifecycle.allow_agent_qemu_args is True
        # Destructive lifecycle operations stay closed even so.
        assert profile.lifecycle.allow_agent_reset is False
        assert profile.lifecycle.allow_agent_force_stop is False
        assert profile.mode == "hybrid"

    def test_several_profiles_are_loaded_and_addressable(
        self, repo: Path, base_image: Path
    ) -> None:
        write_profile(repo, "default", profile_body(base_image))
        write_profile(repo, "other", profile_body(base_image, profile_id="other"))
        config = load(repo)
        assert config.profile_ids() == ["default", "other"]
        assert config.profile("other").profile_id == "other"

    def test_the_profile_id_must_match_the_file_name(self, repo: Path, base_image: Path) -> None:
        write_profile(repo, "default", profile_body(base_image, profile_id="mismatch"))
        with pytest.raises(PaletteError) as info:
            load(repo)
        assert info.value.code is ErrorCode.INVALID_ARGUMENT

    def test_an_unknown_profile_is_refused(self, repo: Path, base_image: Path) -> None:
        write_profile(repo, "default", profile_body(base_image))
        with pytest.raises(PaletteError) as info:
            load(repo).profile("nope")
        assert info.value.code is ErrorCode.INVALID_ARGUMENT
        assert "known_profiles" in info.value.details

    def test_a_default_profile_that_does_not_exist_is_refused(
        self, repo: Path, base_image: Path
    ) -> None:
        write_profile(repo, "default", profile_body(base_image))
        (repo / "palette.toml").write_text('[host]\ndefault_profile = "ghost"\n', encoding="utf-8")
        with pytest.raises(PaletteError) as info:
            load(repo)
        assert "ghost" in info.value.message

    def test_malformed_toml_is_reported_as_configuration_the_operator_supplied(
        self, repo: Path
    ) -> None:
        write_profile(repo, "default", "profile_id = \n")
        with pytest.raises(PaletteError) as info:
            load(repo)
        assert info.value.code is ErrorCode.INVALID_ARGUMENT

    def test_the_runtime_root_resolves_under_the_config_root(
        self, repo: Path, base_image: Path
    ) -> None:
        write_profile(repo, "default", profile_body(base_image))
        assert load(repo).runtime_root == (repo / ".runtime").resolve()

    def test_an_absolute_runtime_root_is_honoured(
        self, repo: Path, base_image: Path, tmp_path: Path
    ) -> None:
        write_profile(repo, "default", profile_body(base_image))
        elsewhere = tmp_path / "elsewhere"
        (repo / "palette.toml").write_text(
            f'[host]\nruntime_root = "{elsewhere.as_posix()}"\n', encoding="utf-8"
        )
        assert load(repo).runtime_root == elsewhere.resolve()


class TestRefusedRelaxations:
    def test_an_accelerator_fallback_may_not_be_enabled(self, repo: Path, base_image: Path) -> None:
        body = profile_body(base_image, qemu="allow_accelerator_fallback = true\n")
        write_profile(repo, "default", body)
        with pytest.raises(PaletteError) as info:
            load(repo)
        assert "fallback" in info.value.message

    @pytest.mark.parametrize(
        "extra",
        ["-mon vc,mode=control", "-chardev socket,id=x,port=4444", "-vnc :0", "-display vnc=:0"],
    )
    def test_operator_arguments_are_taken_at_face_value(
        self, repo: Path, base_image: Path, extra: str
    ) -> None:
        """An operator writing a profile is trusted, and once callers may pass their own
        arguments an argument filter here would be decorative. A second control channel is
        therefore accepted — and flagged when the plan is built, which is what makes the
        broken single-writer guarantee visible instead of silent."""
        body = profile_body(base_image, qemu=f'extra_args = ["{extra}"]\n')
        write_profile(repo, "default", body)
        profile = load(repo).profile(None)
        assert profile.qemu.extra_args == [extra]

    def test_an_unknown_key_is_a_typo_not_a_silent_no_op(
        self, repo: Path, base_image: Path
    ) -> None:
        """A misspelled safety control must fail loudly; silently ignoring it would leave
        the operator believing a restriction was in place."""
        body = profile_body(base_image, lifecycle="allow_agent_resett = true\n")
        write_profile(repo, "default", body)
        with pytest.raises(PaletteError) as info:
            load(repo)
        assert "lifecycle" in info.value.message

    def test_guest_tools_enabled_without_capabilities_is_refused(
        self, repo: Path, base_image: Path
    ) -> None:
        body = profile_body(base_image, guest_tools="enabled = true\ncapabilities = []")
        write_profile(repo, "default", body)
        with pytest.raises(PaletteError):
            load(repo)

    def test_an_unregistered_guest_capability_is_refused(
        self, repo: Path, base_image: Path
    ) -> None:
        body = profile_body(
            base_image, guest_tools='enabled = true\ncapabilities = ["guest.shell"]\n'
        )
        write_profile(repo, "default", body)
        with pytest.raises(PaletteError) as info:
            load(repo)
        assert "unregistered" in info.value.message

    def test_networking_without_restrictions_is_refused(self, repo: Path, base_image: Path) -> None:
        body = profile_body(base_image, network='mode = "restricted-lab"\nrestrict = false\n')
        write_profile(repo, "default", body)
        with pytest.raises(PaletteError):
            load(repo)


class TestShippedProfiles:
    """The profiles in the repository are documentation as much as configuration, so a
    broken one is worse than none. Both must load, and both must be what they claim."""

    def test_the_default_profile_is_permissive_and_says_so(self) -> None:
        profile = load_config(Path(__file__).resolve().parents[2]).profile("default")
        assert profile.mode == "hybrid"
        assert profile.guest_tools.enabled is True
        assert profile.guest_tools.input_methods == ["native_text"]
        assert profile.guest_tools.allowed_apps["text-editor"]["executable"] == "/usr/bin/mousepad"
        assert profile.network.attaches_nic is True
        assert profile.network.mode == "restricted-lab"
        assert profile.lifecycle.allow_agent_start is True
        assert profile.lifecycle.allow_agent_images is True
        assert profile.lifecycle.allow_agent_iso is True
        assert profile.lifecycle.allow_agent_qemu_args is True
        # Even the permissive profile keeps the destructive operations shut.
        assert profile.lifecycle.allow_agent_reset is False
        assert profile.lifecycle.allow_agent_force_stop is False
        assert profile.capture.keep_frames is False
        assert profile.display.keymap == "us"

    def test_the_strict_example_closes_every_switch(self) -> None:
        profile = load_config(Path(__file__).resolve().parents[2]).profile("strict-example")
        assert profile.mode == "visual_only"
        assert profile.network.mode == "offline"
        assert profile.network.attaches_nic is False
        assert profile.guest_tools.enabled is False
        assert profile.lifecycle.allow_agent_start is False
        assert profile.lifecycle.allow_agent_images is False
        assert profile.lifecycle.allow_agent_iso is False
        assert profile.lifecycle.allow_agent_qemu_args is False

    def test_the_two_shipped_profiles_differ_only_in_posture(self) -> None:
        """The strict example is the default machine with the doors shut, not a different
        machine: the same devices, the same resolution, the same timeouts."""
        config = load_config(Path(__file__).resolve().parents[2])
        permissive, strict = config.profile("default"), config.profile("strict-example")
        assert permissive.devices == strict.devices
        assert permissive.display == strict.display
        assert permissive.qemu.accelerator == strict.qemu.accelerator
        assert permissive.qemu.machine == strict.qemu.machine
        assert permissive.capture.max_long_edge == strict.capture.max_long_edge


class TestConfigRootDiscovery:
    def test_the_nearest_ancestor_with_profiles_wins(self, tmp_path: Path) -> None:
        root = tmp_path / "project"
        (root / "profiles").mkdir(parents=True)
        nested = root / "src" / "deep"
        nested.mkdir(parents=True)
        assert find_config_root(nested) == root.resolve()

    def test_the_environment_override_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "explicit"
        root.mkdir()
        monkeypatch.setenv("PALETTE_ROOT", str(root))
        assert find_config_root(tmp_path) == root.resolve()
