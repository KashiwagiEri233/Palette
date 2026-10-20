"""Build verification uses tiny synthetic packages; it never downloads or runs installers."""

from __future__ import annotations

import hashlib
import io
import json

import pycdlib
import pytest
from scripts import build_windows_tools as builder


def test_build_roundtrips_payloads_and_produces_matching_manifest(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    for name in ("Install.cmd", "Install.ps1", "autorun.inf", "README.txt"):
        (source / name).write_text("fixture " + name, encoding="utf-8")
    cache = tmp_path / "cache"
    cache.mkdir()
    payload = cache / "fixture.exe"
    payload.write_bytes(b"not an executable, only a fixture")
    document = {
        "version": "test",
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
    reader = pycdlib.PyCdlib()
    reader.open(str(iso))
    try:
        content = io.BytesIO()
        reader.get_file_from_iso_fp(content, joliet_path="/fixture.exe")
        assert content.getvalue() == payload.read_bytes()
        content = io.BytesIO()
        reader.get_file_from_iso_fp(content, joliet_path="/Install.cmd")
        assert content.getvalue() == (source / "Install.cmd").read_bytes()
    finally:
        reader.close()


def test_offline_build_rejects_corrupt_payload_without_downloading(tmp_path):
    payload = tmp_path / "fixture.exe"
    payload.write_bytes(b"corrupt")
    item = {"name": payload.name, "sha256": "0" * 64, "size_bytes": 7}
    with pytest.raises(ValueError, match="corrupt cached payload"):
        builder.fetch_payload(tmp_path, item, offline=True)
