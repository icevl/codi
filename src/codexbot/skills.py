"""Local skill discovery helpers for Codex and Claude Code.

Resolves available skill names from the agent's skill directories so
Telegram users can quickly see which skills are available for the active
runtime.
"""

from __future__ import annotations

import os
from pathlib import Path


def _codex_skill_roots() -> list[Path]:
    roots: list[Path] = []
    codex_home = os.getenv("CODEX_HOME", "").strip()
    if codex_home:
        roots.append(Path(codex_home).expanduser() / "skills")
    roots.append(Path.home() / ".codex" / "skills")
    return roots


def _claude_skill_roots() -> list[Path]:
    roots: list[Path] = []
    claude_home = os.getenv("CLAUDE_CONFIG_DIR", "").strip()
    if claude_home:
        roots.append(Path(claude_home).expanduser() / "skills")
    roots.append(Path.home() / ".claude" / "skills")
    return roots


def _candidate_skill_roots(runtime: str | None = None) -> list[Path]:
    if runtime == "claude":
        return _claude_skill_roots()
    return _codex_skill_roots()


def discover_skills(runtime: str | None = None) -> list[str]:
    """Return sorted unique skill names for the requested runtime.

    Defaults to Codex skills (preserving the historical, pre-runtime API).
    """
    names: set[str] = set()
    for root in _candidate_skill_roots(runtime):
        if not root.exists() or not root.is_dir():
            continue
        for skill_md in root.rglob("SKILL.md"):
            name = skill_md.parent.name.strip()
            if not name or name.startswith("."):
                continue
            names.add(name)
    return sorted(names)


def skill_invocation_prefix(runtime: str | None) -> str:
    """Return the prefix used to invoke skills in this runtime's TUI."""
    if isinstance(runtime, str) and runtime == "claude":
        return "/"
    return "$"
