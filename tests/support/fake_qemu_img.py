#!/usr/bin/env python3
"""Test double for ``qemu-img``.

It implements the two operations Palette performs — creating an overlay with an explicit
backing file, and reading that overlay's chain back — and stores the chain in the file it
creates, so ``create`` followed by ``info`` behaves like the real tool rather than like a
stub that always agrees.

Anything else exits non-zero, which is what a real ``qemu-img`` does with unsupported
arguments and what the tests need when they check that Palette does not silently accept a
failed overlay creation.
"""

from __future__ import annotations

import json
import os
import sys

VERSION = "11.1.2"
MAGIC = b"QFI\xfb"
VIRTUAL_SIZE = 24 * 1024**3


def main(argv: list[str]) -> int:
    if "--version" in argv or "-V" in argv:
        print(f"qemu-img {VERSION} (fake test double)")
        return 0

    if not argv:
        print("usage: qemu-img command [arguments]", file=sys.stderr)
        return 1

    command = argv[0]
    rest = argv[1:]

    if command == "create":
        return cmd_create(rest)
    if command == "info":
        return cmd_info(rest)
    if command == "resize":
        options = _parse_flags(rest, with_value={"-f"})
        path, size = options["positional"]
        chain = _read_chain(path)
        if not chain or not size.endswith("G"):
            return 1
        chain["virtual_size"] = int(size[:-1]) * 1024**3
        with open(path, "wb") as handle:
            handle.write(MAGIC + json.dumps(chain).encode("utf-8"))
        return 0
    if command == "check":
        options = _parse_flags(rest, with_value={"-f"})
        return 0 if _read_chain(options["positional"][-1]) else 2
    if command == "commit":
        # Palette must never commit a task's writes back into the base image. Failing here
        # turns any accidental call into a loud test failure rather than a silent merge.
        print("qemu-img commit is not supported by the palette test double", file=sys.stderr)
        return 2
    print(f"unknown command {command!r}", file=sys.stderr)
    return 1


def cmd_create(argv: list[str]) -> int:
    options = _parse_flags(argv, with_value={"-f", "-F", "-b", "-o", "-s"})
    positional = options["positional"]
    if len(positional) < 1:
        print("create needs an output filename", file=sys.stderr)
        return 1
    output = (
        positional[-2] if len(positional) >= 2 and positional[-1].endswith("G") else positional[-1]
    )
    if options["flags"].get("-f") == "raw" and "-b" in options["flags"]:
        print("raw images do not support backing files", file=sys.stderr)
        return 1
    chain = {
        "driver": options["flags"].get("-f", "qcow2"),
        "backing": options["flags"].get("-b"),
        "backing_format": options["flags"].get("-F"),
        "virtual_size": int(positional[-1][:-1]) * 1024**3
        if positional[-1].endswith("G")
        else VIRTUAL_SIZE,
    }
    try:
        os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
        if os.path.exists(output):
            print(f"qemu-img: {output} already exists", file=sys.stderr)
            return 1
        with open(output, "wb") as handle:
            handle.write(MAGIC)
            handle.write(json.dumps(chain).encode("utf-8"))
    except OSError as exc:
        print(f"qemu-img: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_info(argv: list[str]) -> int:
    options = _parse_flags(argv, with_value={"-f", "-O", "--output"})
    positional = options["positional"]
    if not positional:
        print("info needs a filename", file=sys.stderr)
        return 1
    target = positional[-1]
    chain = _read_chain(target)
    if chain is None:
        print(f"qemu-img: Could not open '{target}': No such file or directory", file=sys.stderr)
        return 1

    document = {
        "virtual-size": chain.get("virtual_size", VIRTUAL_SIZE),
        "filename": target,
        "format": chain.get("driver", "qcow2"),
        "actual-size": os.path.getsize(target) if os.path.exists(target) else 0,
    }
    if chain.get("backing"):
        document["backing-filename"] = chain["backing"]
        document["full-backing-filename"] = os.path.abspath(
            os.path.join(os.path.dirname(target), chain["backing"])
        )
        if chain.get("backing_format"):
            document["backing-filename-format"] = chain["backing_format"]

    if options["flags"].get("--output") == "json":
        if "--backing-chain" in options["flags"]:
            documents = [document]
            seen = {os.path.abspath(target)}
            while chain.get("backing"):
                target = os.path.abspath(os.path.join(os.path.dirname(target), chain["backing"]))
                if target in seen:
                    return 1
                seen.add(target)
                chain = _read_chain(target)
                if chain is None:
                    return 1
                documents.append({"filename": target, "format": chain.get("driver", "qcow2")})
            print(json.dumps(documents))
        else:
            print(json.dumps(document))
    else:
        for key, value in document.items():
            print(f"{key}: {value}")
    return 0


def _read_chain(path: str) -> dict | None:
    try:
        with open(path, "rb") as handle:
            head = handle.read(4)
            if head != MAGIC:
                return {"driver": "raw"}
            payload = handle.read()
        return json.loads(payload.decode("utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        return {}


def _parse_flags(argv: list[str], *, with_value: set[str]) -> dict:
    """Split argv into options and positional arguments.

    ``--flag=value`` and ``--flag value`` are both accepted, because real qemu-img accepts
    both and a test double that only understood one of them would hide a real bug.
    """
    flags: dict[str, str] = {}
    positional: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token.startswith("-") and "=" in token:
            name, _, value = token.partition("=")
            flags[name] = value
            index += 1
            continue
        if token in with_value and index + 1 < len(argv):
            flags[token] = argv[index + 1]
            index += 2
            continue
        if token.startswith("-") and not token.lstrip("-").isdigit():
            flags[token] = ""
            index += 1
            continue
        positional.append(token)
        index += 1
    return {"flags": flags, "positional": positional}


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
