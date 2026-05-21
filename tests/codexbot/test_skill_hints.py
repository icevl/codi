from __future__ import annotations

import json
from pathlib import Path

import pytest

from codexbot.skill_hints import (
    SkillHintRegistry,
    extract_skill_hints_from_transcript,
    parse_skill_hints_from_instructions,
)


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_parse_skill_hints_from_session_instructions() -> None:
    hints = parse_skill_hints_from_instructions(
        """
        ### Available skills
        - gsd-fast: Execute a trivial task inline (file: /tmp/gsd-fast/SKILL.md)
        - `skill-creator`: Guide for creating effective skills.
        - $openai-docs: Use official OpenAI docs.
        - Discovery: not a skill entry
        - gsd-fast: duplicate should be ignored
        """
    )

    assert [(hint.name, hint.invocation, hint.description) for hint in hints] == [
        ("gsd-fast", "$gsd-fast", "Execute a trivial task inline"),
        ("skill-creator", "$skill-creator", "Guide for creating effective skills."),
        ("openai-docs", "$openai-docs", "Use official OpenAI docs."),
    ]


def test_extract_skill_hints_from_transcript_session_meta(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    _write_jsonl(
        transcript,
        [
            {"type": "event_msg", "payload": {"message": "ignore"}},
            {
                "type": "session_meta",
                "payload": {
                    "instructions": (
                        "### Available skills\n"
                        "- gsd-quick: Execute a quick task with guarantees "
                        "(file: /home/me/gsd-quick/SKILL.md)\n"
                    )
                },
            },
        ],
    )

    hints = extract_skill_hints_from_transcript(transcript)

    assert [(hint.name, hint.invocation, hint.description) for hint in hints] == [
        ("gsd-quick", "$gsd-quick", "Execute a quick task with guarantees")
    ]


@pytest.mark.asyncio
async def test_registry_discovers_transcript_hints_and_reloads_session_cache(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "skill_hints.json"
    transcript = tmp_path / "session.jsonl"
    _write_jsonl(
        transcript,
        [
            {
                "type": "session_meta",
                "payload": {
                    "instructions": "- gsd-debug: Systematic debugging.\n",
                },
            }
        ],
    )

    registry = SkillHintRegistry(cache_path=cache_path)
    hint_set = await registry.discover_now(
        runtime="codex",
        window_id="@1",
        session_id="session-1",
        transcript_path=transcript,
        publish=False,
    )

    assert hint_set.source == "transcript"
    assert [hint.name for hint in hint_set.skills] == ["gsd-debug"]

    reloaded = SkillHintRegistry(cache_path=cache_path)
    cached = reloaded.get_hints("codex", window_id="@2", session_id="session-1")

    assert cached.source == "transcript"
    assert cached.window_id == "@2"
    assert cached.session_id == "session-1"
    assert [hint.invocation for hint in cached.skills] == ["$gsd-debug"]


@pytest.mark.asyncio
async def test_registry_falls_back_to_filesystem_when_transcript_has_no_hints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "codexbot.skill_hints.discover_skills",
        lambda runtime: ["gsd-fast"] if runtime == "codex" else [],
    )

    registry = SkillHintRegistry(cache_path=tmp_path / "skill_hints.json")
    hint_set = await registry.discover_now(
        runtime="codex",
        window_id="@1",
        session_id="session-1",
        transcript_path=tmp_path / "missing.jsonl",
        publish=False,
    )

    assert hint_set.source == "filesystem"
    assert [(hint.name, hint.invocation) for hint in hint_set.skills] == [
        ("gsd-fast", "$gsd-fast")
    ]


def test_registry_returns_empty_for_claude(tmp_path: Path) -> None:
    registry = SkillHintRegistry(cache_path=tmp_path / "skill_hints.json")

    hint_set = registry.get_hints("claude", window_id="@1", session_id="session-1")

    assert hint_set.source == "unsupported"
    assert hint_set.skills == []
