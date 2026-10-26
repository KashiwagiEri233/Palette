"""Build verification uses tiny synthetic packages; it never downloads or runs installers."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path, PurePosixPath

import pycdlib
import pytest
from scripts import build_windows_tools as builder

from palette.tools_media import ToolsManifest


def driver_iso(path: Path, *, missing_catalog: bool = False) -> None:
    """Model the upstream Rock Ridge layout, including files that must be excluded."""
    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=3, rock_ridge="1.09")
    directories: set[str] = set()
    streams: list[io.BytesIO] = []
    try:
        names = ["virtio-win_license.txt"]
        for driver, basename in builder.SETUP_DRIVER_NAMES.items():
            for operating_system in builder.SETUP_DRIVER_OSES:
                names += [
                    f"{driver}/{operating_system}/amd64/{basename}.{ext}"
                    for ext in ("inf", "sys", "cat")
                    if not (missing_catalog and ext == "cat" and operating_system == "w11")
                ]
        names += [
            "NetKVM/w11/amd64/netkvmco.exe",
            "NetKVM/w11/amd64/Readme.md",
            "NetKVM/w11/ARM64/netkvm.inf",
            "viostor/w7/amd64/viostor.inf",
        ]
        for name in names:
            parent = Path(name).parent
            for directory in reversed((parent, *parent.parents)):
                folder = directory.as_posix()
                if folder != "." and folder not in directories:
                    iso.add_directory(iso_path="/" + folder.upper(), rr_name=directory.name)
                    directories.add(folder)
            content = io.BytesIO(("fixture " + name).encode())
            streams.append(content)
            iso.add_fp(
                content,
                len(content.getvalue()),
                iso_path="/" + name.upper().replace("-", "_") + ";1",
                rr_name=Path(name).name,
            )
        iso.write(str(path))
    finally:
        iso.close()
        for content in streams:
            content.close()


def test_build_roundtrips_payloads_and_produces_matching_manifest(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    for name in ("Install.cmd", "Install.ps1", "autorun.inf", "README.txt", *builder.HELPER_FILES):
        (source / name).write_text("fixture " + name, encoding="utf-8")
    cache = tmp_path / "cache"
    cache.mkdir()
    payload = cache / "fixture.exe"
    payload.write_bytes(b"not an executable, only a fixture")
    drivers = cache / "fixture.iso"
    driver_iso(drivers)
    document = {
        "version": "test",
        "setup_drivers": {
            "name": drivers.name,
            "url": "https://example.invalid/not-downloaded",
            "sha256": hashlib.sha256(drivers.read_bytes()).hexdigest(),
            "size_bytes": drivers.stat().st_size,
        },
        "payloads": [
            {
                "name": payload.name,
                "url": "https://example.invalid/not-downloaded",
                "sha256": hashlib.sha256(payload.read_bytes()).hexdigest(),
                "size_bytes": payload.stat().st_size,
            }
        ],
    }
    (source / "payloads.json").write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(builder, "SOURCE", source)
    iso = builder.build_iso(cache, tmp_path / "output", offline=True)
    metadata = json.loads(iso.with_suffix(".json").read_text(encoding="utf-8"))
    assert metadata["sha256"] == hashlib.sha256(iso.read_bytes()).hexdigest()
    assert metadata["size_bytes"] == iso.stat().st_size
    ToolsManifest.model_validate(metadata)
    reader = pycdlib.PyCdlib()
    reader.open(str(iso))
    try:
        content = io.BytesIO()
        reader.get_file_from_iso_fp(content, joliet_path="/fixture.exe")
        assert content.getvalue() == payload.read_bytes()
        content = io.BytesIO()
        reader.get_file_from_iso_fp(content, joliet_path="/Install.cmd")
        assert content.getvalue() == (source / "Install.cmd").read_bytes()
        helper = io.BytesIO()
        reader.get_file_from_iso_fp(helper, joliet_path="/helper-files.json")
        checks = json.loads(helper.getvalue())
        assert checks["protocol_version"] == "1.1"
        for item in checks["files"]:
            actual = (source / item["name"]).read_bytes()
            assert item["sha256"] == hashlib.sha256(actual).hexdigest()
            assert item["size_bytes"] == len(actual)
        expected = [
            "virtio-win_license.txt",
            "NetKVM/w11/amd64/netkvmco.exe",
            "NetKVM/w11/amd64/Readme.md",
        ]
        expected += [
            f"{driver}/{operating_system}/amd64/{basename}.{ext}"
            for driver, basename in builder.SETUP_DRIVER_NAMES.items()
            for operating_system in builder.SETUP_DRIVER_OSES
            for ext in ("inf", "sys", "cat")
        ]
        actual_names = {
            (PurePosixPath(folder) / name).relative_to("/drivers").as_posix()
            for folder, _, names in reader.walk(joliet_path="/drivers")
            for name in names
        }
        assert actual_names == set(expected)
        for name in expected:
            content = io.BytesIO()
            reader.get_file_from_iso_fp(content, joliet_path="/drivers/" + name)
            assert content.getvalue() == ("fixture " + name).encode()
        content = io.BytesIO()
        reader.get_file_from_iso_fp(content, iso_path="/DRIVERS/VIOSTOR/W11/AMD64/VIOSTOR.INF;1")
        assert content.getvalue() == b"fixture viostor/w11/amd64/viostor.inf"
    finally:
        reader.close()


def test_offline_build_rejects_corrupt_payload_without_downloading(tmp_path):
    payload = tmp_path / "fixture.exe"
    payload.write_bytes(b"corrupt")
    item = {"name": payload.name, "sha256": "0" * 64, "size_bytes": 7}
    with pytest.raises(ValueError, match="corrupt cached payload"):
        builder.fetch_payload(tmp_path, item, offline=True)


def test_incomplete_setup_driver_package_fails_before_publishing(tmp_path):
    source = tmp_path / "drivers.iso"
    driver_iso(source, missing_catalog=True)
    with pytest.raises(ValueError, match="Incomplete Windows Setup driver package"):
        builder.extract_setup_drivers(source, tmp_path / "extracted")
