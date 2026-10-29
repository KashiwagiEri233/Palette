"""Check the documentation invariants this repository states but cannot enforce by types.

Three things are true of this project's docs and none of them is checked by a compiler:

* every page exists in English and Chinese, and a change to one is incomplete until the other
  is updated — the pairing is a convention the README states, so it should fail loudly rather
  than drift;
* every relative link resolves, because a docs tree full of dead links is worse than a
  smaller one;
* the files are valid UTF-8 with balanced code fences, since a broken fence silently swallows
  the rest of a page in a renderer.

Run with no arguments from the repository root. Exits non-zero and prints every problem it
found, rather than stopping at the first.
"""

from __future__ import annotations

import pathlib
import re

#: Directories that are not part of the documentation and must not be walked.
SKIP_DIRS = frozenset(
    {".git", ".venv", ".references", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
)

#: Upstream source snapshots keep their own documentation conventions.
UPSTREAM_PREFIXES = ("desktop/vendor/", "ref/")

#: Development plans and coding-agent rules are not bilingual user documentation.
#: Their UTF-8, code fences and relative links are still checked.
UNPAIRED = frozenset(
    {
        "PLAN.md",
        "CLAUDE.md",
        "AGENTS.md",
        "desktop/.better-design/rules.md",
        "desktop/src/.better-design/rules.md",
    }
)

LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
SWITCHER_EN_TO_ZH = "[简体中文]("
SWITCHER_ZH_TO_EN = "[English]("


def is_agent_document(relative: str) -> bool:
    """Skill entrypoints and their references are machine-facing instructions.

    Keep user-facing READMEs, guides and output assets subject to bilingual pairing.
    All files, including agent documents, still receive link/fence/encoding checks.
    """
    if relative in UNPAIRED:
        return True
    parts = pathlib.PurePosixPath(relative).parts
    return (
        len(parts) >= 3
        and parts[0] == "skills"
        and (
            (len(parts) == 3 and parts[2] == "SKILL.md")
            or (len(parts) >= 4 and parts[2] == "references")
        )
    )


def markdown_files(root: pathlib.Path) -> list[pathlib.Path]:
    """The markdown this repository *ships*.

    Enumerated through git rather than by walking the tree, because most of what sits under
    the working directory is not documentation: ``.runtime/`` holds instance overlays, audit
    trails and test screenshots, and ``.references/`` is a third-party checkout. All of it is
    ignored, so git provides the candidate set without holding scratch notes to the bilingual
    rule. Upstream source snapshots are excluded from both enumeration paths.

    Falls back to a filtered walk outside a git work tree, so the script still works if it is
    ever copied somewhere on its own.
    """
    tracked = _git_tracked_markdown(root)
    if tracked is not None:
        # Vendored upstream documentation retains its original language and links.
        return [
            root / relative for relative in tracked if not relative.startswith(UPSTREAM_PREFIXES)
        ]
    return sorted(
        path
        for path in root.rglob("*.md")
        if not any(part in SKIP_DIRS for part in path.parts)
        and not path.relative_to(root).as_posix().startswith(UPSTREAM_PREFIXES)
    )


def _git_tracked_markdown(root: pathlib.Path) -> list[str] | None:
    import subprocess

    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "*.md"],  # noqa: S607 - resolved through PATH by design
            cwd=str(root),
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return sorted(name for name in result.stdout.decode("utf-8").split("\0") if name)


def check(path: pathlib.Path, root: pathlib.Path) -> list[str]:
    relative = path.relative_to(root).as_posix()
    problems: list[str] = []

    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        return [f"{relative}: not valid UTF-8 ({exc})"]

    if "�" in text:
        problems.append(f"{relative}: contains U+FFFD replacement characters")

    fences = text.count("```")
    if fences % 2:
        problems.append(f"{relative}: unbalanced code fences ({fences})")

    if not is_agent_document(relative):
        wanted = SWITCHER_ZH_TO_EN if relative.endswith(".zh-CN.md") else SWITCHER_EN_TO_ZH
        if wanted not in text:
            problems.append(f"{relative}: missing the language switcher for {wanted.strip('[')}")

    for target in LINK.findall(text):
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        target_path = target.split("#")[0]
        if target_path and not (path.parent / target_path).resolve().exists():
            problems.append(f"{relative}: broken link -> {target}")

    return problems


def check_pairing(root: pathlib.Path, files: list[pathlib.Path]) -> list[str]:
    present = {path.relative_to(root).as_posix() for path in files}
    problems: list[str] = []
    for relative in sorted(present):
        if is_agent_document(relative):
            continue
        if relative.endswith(".zh-CN.md"):
            counterpart = relative[: -len(".zh-CN.md")] + ".md"
            if counterpart not in present:
                problems.append(f"{relative}: no English counterpart ({counterpart})")
        elif relative.endswith(".md"):
            counterpart = relative[: -len(".md")] + ".zh-CN.md"
            if counterpart not in present:
                problems.append(f"{relative}: no Chinese counterpart ({counterpart})")
    return problems


def main() -> int:
    root = pathlib.Path(__file__).resolve().parents[1]
    files = markdown_files(root)

    problems: list[str] = []
    for path in files:
        problems.extend(check(path, root))
    problems.extend(check_pairing(root, files))

    if problems:
        print(f"{len(problems)} documentation problem(s):")
        for problem in problems:
            print(" -", problem)
        return 1

    print(
        f"{len(files)} markdown files: valid UTF-8, balanced fences, switchers present, "
        "every relative link resolves, every page paired"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
