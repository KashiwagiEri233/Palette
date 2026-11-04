"""Local display preferences cannot activate the experiment accidentally."""

import json

import pytest

from palette.display_settings import DisplaySettings
from palette.errors import ErrorCode, PaletteError


def test_absent_preference_preserves_configuration_and_updates_survive_reload(tmp_path):
    settings = DisplaySettings(tmp_path)
    assert settings.mode == "inherit"
    assert not (tmp_path / "display-settings.json").exists()
    settings.update({"mode": "low_latency"})
    assert DisplaySettings(tmp_path).mode == "low_latency"
    settings.update({"mode": "compatibility"})
    assert DisplaySettings(tmp_path).mode == "compatibility"
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"mode": "inherit"},
        {"mode": []},
        {"mode": True},
        {"mode": "low_latency", "helper": "arbitrary"},
    ],
)
def test_only_the_two_explicit_modes_can_be_saved(tmp_path, payload):
    settings = DisplaySettings(tmp_path)
    with pytest.raises(PaletteError) as error:
        settings.update(payload)
    assert error.value.code == ErrorCode.INVALID_ARGUMENT
    assert settings.mode == "inherit"
    assert not (tmp_path / "display-settings.json").exists()


@pytest.mark.parametrize(
    "data",
    [b"broken", b"x" * 4097, json.dumps({"schema_version": 2, "mode": "low_latency"}).encode()],
)
def test_invalid_saved_preferences_choose_compatibility_with_a_reason(tmp_path, data):
    (tmp_path / "display-settings.json").write_bytes(data)
    settings = DisplaySettings(tmp_path)
    assert settings.mode == "compatibility"
    assert settings.reason


def test_failed_atomic_save_preserves_previous_value(tmp_path, monkeypatch):
    settings = DisplaySettings(tmp_path)
    settings.update({"mode": "compatibility"})

    def fail_replace(*args):
        raise OSError("disk failure")

    monkeypatch.setattr("palette.display_settings.os.replace", fail_replace)
    with pytest.raises(PaletteError):
        settings.update({"mode": "low_latency"})
    assert settings.mode == "compatibility"
    assert DisplaySettings(tmp_path).mode == "compatibility"
    assert list(tmp_path.glob("*.tmp")) == []


def test_directory_sync_failure_reports_the_committed_value(tmp_path, monkeypatch):
    def fail_sync(*args):
        raise OSError("directory sync failed after replace")

    settings = DisplaySettings(tmp_path)
    monkeypatch.setattr("palette.display_settings._sync_directory", fail_sync)
    settings.update({"mode": "low_latency"})
    assert settings.mode == "low_latency" and settings.reason
    assert DisplaySettings(tmp_path).mode == "low_latency"


def test_denied_preference_path_falls_back_without_following_it(tmp_path, monkeypatch):
    def deny_preference(*args):
        raise PaletteError(ErrorCode.PATH_DENIED, "unsafe preference path")

    monkeypatch.setattr("palette.display_settings.PathGuard.resolve", deny_preference)
    settings = DisplaySettings(tmp_path)
    assert settings.mode == "compatibility" and settings.reason
