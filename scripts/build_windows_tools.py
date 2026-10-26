"""Build the offline Windows tools ISO from pinned, verified upstream installers."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import time
import urllib.request
from pathlib import Path
from typing import TypedDict, cast

import pycdlib

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "guest" / "windows"
HELPER_FILES = ("Build-Agent.ps1", "Palette.Agent.cs", "Palette.Native.cs", "Palette.Operations.cs")
ISO_NAME = "palette-tools-windows-x86_64.iso"


class Payload(TypedDict):
    name: str
    url: str
    sha256: str
    size_bytes: int


class PayloadDocument(TypedDict):
    version: str
    payloads: list[Payload]


def checked_payload(path: Path, item: Payload) -> bool:
    if not path.is_file() or path.stat().st_size != item["size_bytes"]:
        return False
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest() == item["sha256"]


def fetch_payload(cache: Path, item: Payload, *, offline: bool) -> Path:
    path = cache / item["name"]
    if checked_payload(path, item):
        return path
    if offline:
        raise ValueError(f"Missing or corrupt cached payload: {path}")
    url = item["url"]
    if not url.startswith(("https://fedorapeople.org/", "https://www.spice-space.org/")):
        raise ValueError("payload source must be an official HTTPS download")
    cache.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".partial")
    started = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=30) as response, partial.open("wb") as output:  # noqa: S310
            total = 0
            while chunk := response.read(64 * 1024):
                total += len(chunk)
                if total > item["size_bytes"] or time.monotonic() - started > 300:
                    raise ValueError("payload download exceeded its size or time limit")
                output.write(chunk)
        if not checked_payload(partial, item):
            raise ValueError(f"Payload checksum failed: {item['name']}")
        partial.replace(path)
    finally:
        partial.unlink(missing_ok=True)
    return path


def build_iso(cache: Path, output: Path, *, offline: bool = False) -> Path:
    payload_document = cast(
        PayloadDocument, json.loads((SOURCE / "payloads.json").read_text(encoding="utf-8"))
    )
    files = [
        (SOURCE / name, name)
        for name in (
            "Install.cmd",
            "Install.ps1",
            "autorun.inf",
            "README.txt",
            "payloads.json",
            *HELPER_FILES,
        )
    ]
    files += [
        (fetch_payload(cache, item, offline=offline), item["name"])
        for item in payload_document["payloads"]
    ]
    output.mkdir(parents=True, exist_ok=True)
    path = output / ISO_NAME
    partial = output / (ISO_NAME + ".partial")
    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=3, joliet=3, vol_ident="PALETTE_TOOLS")
    helper_manifest = json.dumps(
        {
            "protocol_version": "1.1",
            "files": [
                {
                    "name": name,
                    "size_bytes": (SOURCE / name).stat().st_size,
                    "sha256": hashlib.sha256((SOURCE / name).read_bytes()).hexdigest(),
                }
                for name in HELPER_FILES
            ],
        },
        indent=2,
    ).encode("utf-8")
    helper_stream = io.BytesIO(helper_manifest)
    try:
        for index, (file, name) in enumerate(files):
            # Joliet preserves the user-visible file names used by the guest installer.
            iso.add_file(str(file), iso_path=f"/FILE{index:03d}.DAT;1", joliet_path="/" + name)
        iso.add_fp(
            helper_stream,
            len(helper_manifest),
            iso_path="/HELPER.JSON;1",
            joliet_path="/helper-files.json",
        )
        iso.write(str(partial))
    finally:
        iso.close()
        helper_stream.close()
    # Read back every embedded payload, including scripts, before publishing the ISO.
    verify = pycdlib.PyCdlib()
    verify.open(str(partial))
    try:
        with verify.open_file_from_iso(joliet_path="/helper-files.json") as embedded:
            if embedded.read() != helper_manifest:
                raise ValueError("ISO helper manifest verification failed")
        for original, name in files:
            with (
                verify.open_file_from_iso(joliet_path="/" + name) as embedded,
                original.open("rb") as source,
            ):
                if (
                    hashlib.file_digest(embedded, "sha256").digest()
                    != hashlib.file_digest(source, "sha256").digest()
                ):
                    raise ValueError(f"ISO content verification failed: {name}")
    finally:
        verify.close()
    with partial.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    manifest = {
        "schema_version": 1,
        "guest_os": "windows",
        "arch": "x86_64",
        "version": payload_document["version"],
        "sha256": digest,
        "size_bytes": partial.stat().st_size,
    }
    manifest_partial = output / (ISO_NAME + ".json.partial")
    manifest_partial.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    partial.replace(path)
    manifest_partial.replace(path.with_suffix(".json"))
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=ROOT / ".runtime" / "guest-tools-cache")
    parser.add_argument("--output", type=Path, default=ROOT / "images" / "guest-tools")
    parser.add_argument("--offline", action="store_true", help="use verified cached payloads only")
    args = parser.parse_args()
    print(build_iso(args.cache.resolve(), args.output.resolve(), offline=args.offline))


if __name__ == "__main__":
    main()
