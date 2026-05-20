import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from codexbot import slash_commands as slash_module
from codexbot.slash_commands import (
    SlashCommandRegistry,
    extract_help_stdout_from_transcript,
    parse_slash_commands_from_help,
)


def test_parse_slash_commands_from_markdown_lines() -> None:
    commands = parse_slash_commands_from_help(
        """
        Available commands:
        /clear - Clear conversation history
        - /compact: Compact the current conversation
        • `/model`  Switch AI model
        """
    )

    assert [(c.command, c.description) for c in commands] == [
        ("/clear", "Clear conversation history"),
        ("/compact", "Compact the current conversation"),
        ("/model", "Switch AI model"),
    ]


def test_parse_slash_commands_from_table() -> None:
    commands = parse_slash_commands_from_help(
        """
        | Command | Description |
        | --- | --- |
        | `/permissions` | Review tool permissions |
        | `/doctor` | Check installation |
        """
    )

    assert [(c.command, c.description) for c in commands] == [
        ("/permissions", "Review tool permissions"),
        ("/doctor", "Check installation"),
    ]


def test_extracts_claude_split_local_command_stdout(tmp_path) -> None:
    transcript = tmp_path / "claude.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "system",
                        "subtype": "local_command",
                        "content": "<command-name>/help</command-name>",
                    }
                ),
                json.dumps(
                    {
                        "type": "system",
                        "subtype": "local_command",
                        "content": (
                            "<local-command-stdout>"
                            "/clear - Clear conversation\n"
                            "/status - Show status"
                            "</local-command-stdout>"
                        ),
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert extract_help_stdout_from_transcript(transcript) == (
        "/clear - Clear conversation\n/status - Show status"
    )


@pytest.mark.asyncio
async def test_registry_discovers_commands_once_per_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    transcript = tmp_path / "codex.jsonl"
    cache_path = tmp_path / "slash_commands.json"
    send_calls: list[tuple[str, str]] = []

    async def fake_send(window_id: str, text: str) -> bool:
        send_calls.append((window_id, text))
        transcript.write_text(
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "<command-name>/help</command-name>"
                                    "<local-command-stdout>"
                                    "/clear - Clear conversation\n"
                                    "/model - Switch model"
                                    "</local-command-stdout>"
                                ),
                            }
                        ]
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return True

    monkeypatch.setattr(
        slash_module.tmux_manager,
        "send_keys",
        AsyncMock(side_effect=fake_send),
    )
    registry = SlashCommandRegistry(
        cache_path=cache_path,
        discovery_timeout_seconds=1.0,
        discovery_poll_interval_seconds=0.01,
    )

    registry.schedule_discovery(
        runtime="codex",
        window_id="@1",
        session_id="session-1",
        transcript_path=transcript,
    )
    for _ in range(100):
        result = registry.get_commands("codex", window_id="@1", session_id="session-1")
        if result.source == "discovered":
            break
        await asyncio.sleep(0.01)

    result = registry.get_commands("codex", window_id="@1", session_id="session-1")
    assert result.source == "discovered"
    assert [(c.command, c.description) for c in result.commands] == [
        ("/clear", "Clear conversation"),
        ("/model", "Switch model"),
    ]

    registry.schedule_discovery(
        runtime="codex",
        window_id="@1",
        session_id="session-1",
        transcript_path=transcript,
    )
    await asyncio.sleep(0.02)
    assert send_calls == [("@1", "/help")]

    reloaded = SlashCommandRegistry(cache_path=cache_path)
    assert reloaded.get_commands("codex", session_id="session-1").source == "discovered"
