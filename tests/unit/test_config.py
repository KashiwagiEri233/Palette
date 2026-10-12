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
        write_profile(repo, "default", profile_body(base_image))
        config = load(repo)
        profile = config.profile(None)
        assert profile.profile_id == "default"
        # The safety-relevant defaults are the point: a profile that says nothing about
        # them still gets the safe answer.
        assert profile.network.mode == "offline"
        assert profile.qemu.allow_accelerator_fallback is False
        assert profile.guest_tools.enabled is False
        assert profile.lifecycle.allow_agent_reset is False
        assert profile.mode == "visual_only"

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
    def test_a_profile_may_not_add_a_second_control_channel(
        self, repo: Path, base_image: Path, extra: str
    ) -> None:
        body = profile_body(base_image, qemu=f'extra_args = ["{extra}"]\n')
        write_profile(repo, "default", body)
        with pytest.raises(PaletteError) as info:
            load(repo)
        assert "monitor or display" in info.value.message

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
        body = profile_body(base_image, guest_tools="enabled = true\n")
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


class TestHybridProfile:
    def test_the_shipped_hybrid_example_loads_and_stays_operator_only(self) -> None:
        """The example file is documentation as much as configuration, so it has to be
        valid: a broken example is worse than no example."""
        config = load_config(Path(__file__).resolve().parents[2])
        profile = config.profile("hybrid-example")
        assert profile.mode == "hybrid"
        assert profile.guest_tools.enabled is True
        assert set(profile.guest_tools.capabilities) == {"guest.input_text", "guest.launch_app"}
        assert profile.guest_tools.allowed_apps["text-editor"]["executable"] == "/usr/bin/mousepad"
        # Enabling the guest channel must not hand the agent the lifecycle.
        assert profile.lifecycle.allow_agent_start is False
        assert profile.lifecycle.allow_agent_reset is False
        assert profile.network.mode == "offline"

    def test_the_shipped_default_profile_is_visual_only_and_offline(self) -> None:
        config = load_config(Path(__file__).resolve().parents[2])
        profile = config.profile("default")
        assert profile.mode == "visual_only"
        assert profile.guest_tools.enabled is False
        assert profile.network.mode == "offline"
        assert profile.capture.keep_frames is False
        assert profile.display.keymap == "us"


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
