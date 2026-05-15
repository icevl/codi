"""Tests for local skill discovery helpers (Codex + Claude)."""

from pathlib import Path

from codexbot.skills import discover_skills, skill_invocation_prefix


def test_discover_skills_from_codex_home(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "codex-home" / "skills"
    (root / "alpha").mkdir(parents=True)
    (root / "alpha" / "SKILL.md").write_text("# alpha\n", encoding="utf-8")
    (root / "beta").mkdir(parents=True)
    (root / "beta" / "SKILL.md").write_text("# beta\n", encoding="utf-8")

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert discover_skills() == ["alpha", "beta"]


def test_discover_skills_dedupes_and_skips_hidden(monkeypatch, tmp_path: Path) -> None:
    codex_home_root = tmp_path / "codex-home" / "skills"
    user_root = tmp_path / ".codex" / "skills"

    (codex_home_root / "dup").mkdir(parents=True)
    (codex_home_root / "dup" / "SKILL.md").write_text("# dup\n", encoding="utf-8")
    (user_root / "dup").mkdir(parents=True)
    (user_root / "dup" / "SKILL.md").write_text("# dup\n", encoding="utf-8")
    (user_root / ".internal").mkdir(parents=True)
    (user_root / ".internal" / "SKILL.md").write_text("# hidden\n", encoding="utf-8")

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert discover_skills() == ["dup"]


def test_discover_skills_for_claude_runtime(monkeypatch, tmp_path: Path) -> None:
    """Claude runtime must look at ~/.claude/skills, not ~/.codex/skills."""
    claude_root = tmp_path / ".claude" / "skills"
    (claude_root / "review").mkdir(parents=True)
    (claude_root / "review" / "SKILL.md").write_text("# review\n", encoding="utf-8")

    codex_root = tmp_path / ".codex" / "skills"
    (codex_root / "gsd-quick").mkdir(parents=True)
    (codex_root / "gsd-quick" / "SKILL.md").write_text("# gsd\n", encoding="utf-8")

    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    # Claude runtime sees Claude skills only.
    assert discover_skills(runtime="claude") == ["review"]
    # Codex runtime sees Codex skills only.
    assert discover_skills(runtime="codex") == ["gsd-quick"]
    # Default (no runtime) matches the legacy Codex-only behavior.
    assert discover_skills() == ["gsd-quick"]


def test_skill_invocation_prefix() -> None:
    assert skill_invocation_prefix("codex") == "$"
    assert skill_invocation_prefix("claude") == "/"
    assert skill_invocation_prefix(None) == "$"
    # Non-string values must not be treated as Claude.
    assert skill_invocation_prefix(object()) == "$"  # type: ignore[arg-type]
