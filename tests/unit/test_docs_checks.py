"""Agent-rule exemptions must preserve the remaining documentation checks."""

import runpy
from pathlib import Path

import pytest

CHECKER = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts" / "check_docs.py"))
AGENT_RULES = [
    "AGENTS.md",
    "desktop/.better-design/rules.md",
    "desktop/src/.better-design/rules.md",
]


@pytest.mark.parametrize("relative", AGENT_RULES)
def test_agent_rules_do_not_need_translation(tmp_path: Path, relative: str) -> None:
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Development rules\n", encoding="utf-8")
    assert CHECKER["check"](path, tmp_path) == []
    assert CHECKER["check_pairing"](tmp_path, [path]) == []


@pytest.mark.parametrize("relative", AGENT_RULES)
def test_agent_rules_still_validate_links_and_fences(tmp_path: Path, relative: str) -> None:
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[Missing](missing.md)\n```\n", encoding="utf-8")
    problems = CHECKER["check"](path, tmp_path)
    assert len(problems) == 2
    assert any("broken link" in problem for problem in problems)
    assert any("unbalanced code fences" in problem for problem in problems)


@pytest.mark.parametrize(
    "relative",
    [
        "docs/user-guide.md",
        "desktop/.better-design/notes.md",
        "desktop/src/.better-design/notes.md",
    ],
)
def test_other_pages_still_require_translation(tmp_path: Path, relative: str) -> None:
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text("# Documentation\n", encoding="utf-8")
    assert any("language switcher" in problem for problem in CHECKER["check"](path, tmp_path))
    assert any(
        "Chinese counterpart" in problem for problem in CHECKER["check_pairing"](tmp_path, [path])
    )


@pytest.mark.parametrize("tracked", [True, False])
def test_markdown_enumeration_excludes_only_upstream_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tracked: bool
) -> None:
    owned = ["README.md", "docs/user-guide.md", "docs/ref/guide.md", "desktop/vendor-notes.md"]
    upstream = ["ref/pi/README.md", "ref/pi/.pi/prompts/cl.md", "desktop/vendor/pi/README.md"]
    for relative in owned + upstream:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Documentation\n", encoding="utf-8")

    monkeypatch.setitem(
        CHECKER["markdown_files"].__globals__,
        "_git_tracked_markdown",
        lambda _root: sorted(owned + upstream) if tracked else None,
    )
    files = CHECKER["markdown_files"](tmp_path)
    assert sorted(path.relative_to(tmp_path).as_posix() for path in files) == sorted(owned)
    problems = CHECKER["check_pairing"](tmp_path, files)
    assert len(problems) == len(owned)
    assert all("no Chinese counterpart" in problem for problem in problems)
