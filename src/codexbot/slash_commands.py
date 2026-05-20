"""Runtime slash-command discovery and caching.

The web UI needs command hints that track the installed Codex/Claude CLI
instead of a hardcoded frontend list. Discovery is intentionally one-shot per
agent session: once a new session is ready, Codi sends `/help`, parses the
local-command stdout from the transcript, and caches the result.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .config import config
from .tmux_manager import tmux_manager
from .utils import atomic_write_json

logger = logging.getLogger(__name__)

EventPublisher = Callable[[dict[str, Any]], Awaitable[None]]

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_COMMAND_NAME_RE = re.compile(r"<command-name>(.*?)</command-name>", re.DOTALL)
_LOCAL_STDOUT_RE = re.compile(
    r"<local-command-stdout>(.*?)</local-command-stdout>",
    re.DOTALL,
)
_SLASH_COMMAND_RE = re.compile(r"^`?(?P<command>/[A-Za-z][\w:-]*)`?(?P<rest>.*)$")
_SEPARATOR_RE = re.compile(r"^\s*[-:–—]\s*")
_BULLET_RE = re.compile(r"^\s*(?:[-*•·]|\d+[.)])\s+")


@dataclass(frozen=True)
class SlashCommand:
    command: str
    description: str = ""


@dataclass(frozen=True)
class SlashCommandSet:
    runtime: str
    commands: list[SlashCommand]
    source: str
    updated_at: float | None = None
    window_id: str | None = None
    session_id: str | None = None

    def to_response(self) -> dict[str, Any]:
        return {
            "runtime": self.runtime,
            "window_id": self.window_id,
            "session_id": self.session_id,
            "commands": [asdict(command) for command in self.commands],
            "source": self.source,
            "updated_at": self.updated_at,
        }


DEFAULT_COMMANDS: dict[str, list[SlashCommand]] = {
    "codex": [
        SlashCommand("/clear", "Clear conversation history"),
        SlashCommand("/new", "Start a new conversation"),
        SlashCommand("/compact", "Compact conversation context"),
        SlashCommand("/status", "Show current agent status"),
        SlashCommand("/cost", "Show token and cost usage"),
        SlashCommand("/help", "Show Codex help"),
        SlashCommand("/memory", "Edit project memory instructions"),
        SlashCommand("/model", "Switch AI model"),
        SlashCommand("/plan", "Draft or update a plan"),
        SlashCommand("/skills", "List Codex skills"),
    ],
    "claude": [
        SlashCommand("/clear", "Clear conversation history"),
        SlashCommand("/compact", "Compact conversation context"),
        SlashCommand("/config", "Open Claude Code configuration"),
        SlashCommand("/cost", "Show token and cost usage"),
        SlashCommand("/doctor", "Check Claude Code installation"),
        SlashCommand("/help", "Show Claude Code help"),
        SlashCommand("/init", "Initialize project instructions"),
        SlashCommand("/login", "Sign in to Claude Code"),
        SlashCommand("/logout", "Sign out of Claude Code"),
        SlashCommand("/mcp", "Manage MCP servers"),
        SlashCommand("/memory", "Edit Claude memory files"),
        SlashCommand("/model", "Switch AI model"),
        SlashCommand("/permissions", "Review tool permissions"),
        SlashCommand("/resume", "Resume a previous conversation"),
        SlashCommand("/review", "Request code review"),
        SlashCommand("/status", "Show current agent status"),
    ],
}


def _normalize_runtime(runtime: str | None) -> str:
    value = (runtime or "codex").strip().lower()
    return value if value in DEFAULT_COMMANDS else "codex"


def _normalize_command(raw: str) -> str:
    command = raw.strip().strip("`").rstrip(":")
    if not command.startswith("/"):
        command = f"/{command}"
    return command


def _clean_description(raw: str) -> str:
    text = _ANSI_RE.sub("", raw).strip()
    text = _SEPARATOR_RE.sub("", text).strip()
    text = text.strip("`").strip()
    return re.sub(r"\s+", " ", text)


def _add_command(
    commands: list[SlashCommand],
    seen: dict[str, int],
    command: str,
    description: str,
) -> None:
    command = _normalize_command(command)
    description = _clean_description(description)
    if command in seen:
        idx = seen[command]
        if description and not commands[idx].description:
            commands[idx] = SlashCommand(command, description)
        return
    seen[command] = len(commands)
    commands.append(SlashCommand(command, description))


def parse_slash_commands_from_help(stdout: str) -> list[SlashCommand]:
    """Extract slash commands from Codex/Claude help text."""
    text = _ANSI_RE.sub("", stdout or "")
    commands: list[SlashCommand] = []
    seen: dict[str, int] = {}

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("|") and line.endswith("|"):
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if all(set(cell) <= {"-", ":", " "} for cell in cells):
                continue
            command_cell = next(
                (
                    cell
                    for cell in cells
                    if _SLASH_COMMAND_RE.match(cell.strip().strip("`"))
                ),
                "",
            )
            if command_cell:
                desc = next(
                    (
                        cell
                        for cell in cells
                        if cell != command_cell
                        and cell
                        and not _SLASH_COMMAND_RE.match(cell.strip().strip("`"))
                        and cell.lower() not in {"command", "description"}
                    ),
                    "",
                )
                _add_command(commands, seen, command_cell, desc)
            continue

        line = _BULLET_RE.sub("", line)
        match = _SLASH_COMMAND_RE.match(line)
        if not match:
            continue

        command = match.group("command")
        rest = match.group("rest").strip()
        description = ""
        if rest:
            if "  " in rest:
                description = re.split(r"\s{2,}", rest, maxsplit=1)[-1]
            else:
                description = rest
        _add_command(commands, seen, command, description)

    return commands


def _entry_text(data: dict[str, Any]) -> str:
    content = data.get("content")
    if isinstance(content, str):
        return content

    message = data.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content) if content else ""

    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            value = item.get("text")
            if isinstance(value, str):
                parts.append(value)
    return "\n".join(parts)


def _extract_help_stdout_from_text(
    text: str,
    *,
    active_command: str | None,
) -> tuple[str | None, str | None]:
    command = active_command
    cmd_match = _COMMAND_NAME_RE.search(text)
    if cmd_match:
        command = cmd_match.group(1).strip()

    stdout_match = _LOCAL_STDOUT_RE.search(text)
    if stdout_match and command == "/help":
        return stdout_match.group(1), command
    return None, command


def extract_help_stdout_from_transcript(
    transcript_path: Path,
    *,
    start_offset: int = 0,
) -> str | None:
    """Return the newest `/help` local-command stdout after start_offset."""
    if not transcript_path.exists():
        return None

    last_command: str | None = None
    last_help_stdout: str | None = None
    try:
        with transcript_path.open("r", encoding="utf-8") as f:
            if start_offset > 0:
                f.seek(start_offset)
            for line in f:
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                stdout, last_command = _extract_help_stdout_from_text(
                    _entry_text(data),
                    active_command=last_command,
                )
                if stdout is not None:
                    last_help_stdout = stdout
                    last_command = None
    except OSError as exc:
        logger.debug(
            "failed to read slash-command transcript %s: %s", transcript_path, exc
        )
        return None

    return last_help_stdout


class SlashCommandRegistry:
    def __init__(
        self,
        *,
        cache_path: Path | None = None,
        discovery_timeout_seconds: float = 8.0,
        discovery_poll_interval_seconds: float = 0.5,
    ) -> None:
        self.cache_path = cache_path or (config.config_dir / "slash_commands.json")
        self.discovery_timeout_seconds = discovery_timeout_seconds
        self.discovery_poll_interval_seconds = discovery_poll_interval_seconds
        self._runtime_commands: dict[str, SlashCommandSet] = {}
        self._session_commands: dict[str, SlashCommandSet] = {}
        self._inflight: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._event_publisher: EventPublisher | None = None
        self._load_cache()

    def set_event_publisher(self, publisher: EventPublisher | None) -> None:
        self._event_publisher = publisher

    def defaults_for(self, runtime: str | None) -> SlashCommandSet:
        runtime_name = _normalize_runtime(runtime)
        return SlashCommandSet(
            runtime=runtime_name,
            commands=list(DEFAULT_COMMANDS[runtime_name]),
            source="fallback",
            updated_at=None,
        )

    def get_commands(
        self,
        runtime: str | None,
        *,
        window_id: str | None = None,
        session_id: str | None = None,
    ) -> SlashCommandSet:
        runtime_name = _normalize_runtime(runtime)
        if session_id and session_id in self._session_commands:
            command_set = self._session_commands[session_id]
        else:
            command_set = self._runtime_commands.get(runtime_name) or self.defaults_for(
                runtime_name
            )
        return SlashCommandSet(
            runtime=runtime_name,
            commands=list(command_set.commands),
            source=command_set.source,
            updated_at=command_set.updated_at,
            window_id=window_id,
            session_id=session_id,
        )

    def schedule_discovery(
        self,
        *,
        runtime: str | None,
        window_id: str,
        session_id: str | None,
        transcript_path: Path | str | None,
        force: bool = False,
    ) -> None:
        runtime_name = _normalize_runtime(runtime)
        if not window_id or not session_id or transcript_path is None:
            return
        if not force and session_id in self._session_commands:
            return

        key = (runtime_name, session_id)
        task = self._inflight.get(key)
        if task is not None and not task.done():
            return

        self._inflight[key] = asyncio.create_task(
            self._run_discovery(
                runtime=runtime_name,
                window_id=window_id,
                session_id=session_id,
                transcript_path=Path(transcript_path),
            ),
            name=f"slash-command-discovery:{runtime_name}:{window_id}",
        )

    async def _run_discovery(
        self,
        *,
        runtime: str,
        window_id: str,
        session_id: str,
        transcript_path: Path,
    ) -> None:
        key = (runtime, session_id)
        try:
            start_offset = (
                transcript_path.stat().st_size if transcript_path.exists() else 0
            )
            ok = await tmux_manager.send_keys(window_id, "/help")
            if not ok:
                logger.warning(
                    "slash_command_discovery_send_failed runtime=%s window=%s session=%s",
                    runtime,
                    window_id,
                    session_id,
                )
                self._remember_session_fallback(runtime, window_id, session_id)
                return

            deadline = time.monotonic() + self.discovery_timeout_seconds
            while time.monotonic() < deadline:
                stdout = await asyncio.to_thread(
                    extract_help_stdout_from_transcript,
                    transcript_path,
                    start_offset=start_offset,
                )
                if stdout:
                    commands = parse_slash_commands_from_help(stdout)
                    if commands:
                        self._remember_discovered(
                            runtime,
                            window_id,
                            session_id,
                            commands,
                        )
                        logger.info(
                            "slash_command_discovery_ok runtime=%s window=%s session=%s commands=%d",
                            runtime,
                            window_id,
                            session_id,
                            len(commands),
                        )
                        return
                await asyncio.sleep(self.discovery_poll_interval_seconds)

            logger.warning(
                "slash_command_discovery_timeout runtime=%s window=%s session=%s path=%s",
                runtime,
                window_id,
                session_id,
                transcript_path,
            )
            self._remember_session_fallback(runtime, window_id, session_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "slash_command_discovery_failed runtime=%s window=%s session=%s: %s",
                runtime,
                window_id,
                session_id,
                exc,
            )
            self._remember_session_fallback(runtime, window_id, session_id)
        finally:
            self._inflight.pop(key, None)

    def _remember_discovered(
        self,
        runtime: str,
        window_id: str,
        session_id: str,
        commands: list[SlashCommand],
    ) -> None:
        now = time.time()
        command_set = SlashCommandSet(
            runtime=runtime,
            commands=commands,
            source="discovered",
            updated_at=now,
            window_id=window_id,
            session_id=session_id,
        )
        self._runtime_commands[runtime] = SlashCommandSet(
            runtime=runtime,
            commands=commands,
            source="discovered",
            updated_at=now,
        )
        self._session_commands[session_id] = command_set
        self._save_cache()
        self._publish_changed(runtime, window_id, session_id, "discovered")

    def _remember_session_fallback(
        self,
        runtime: str,
        window_id: str,
        session_id: str,
    ) -> None:
        command_set = self.get_commands(runtime, window_id=window_id)
        self._session_commands[session_id] = SlashCommandSet(
            runtime=runtime,
            commands=list(command_set.commands),
            source=command_set.source,
            updated_at=command_set.updated_at,
            window_id=window_id,
            session_id=session_id,
        )
        self._save_cache()
        self._publish_changed(runtime, window_id, session_id, command_set.source)

    def _publish_changed(
        self,
        runtime: str,
        window_id: str,
        session_id: str,
        source: str,
    ) -> None:
        publisher = self._event_publisher
        if publisher is None:
            return
        event = {
            "type": "slash_commands_changed",
            "runtime": runtime,
            "window_id": window_id,
            "session_id": session_id,
            "source": source,
        }

        async def _publish() -> None:
            await publisher(event)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("no running loop for slash command event")
            return
        loop.create_task(_publish(), name="slash-command-event")

    def _load_cache(self) -> None:
        try:
            data = json.loads(self.cache_path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return

        runtime_commands = data.get("runtime_commands")
        if isinstance(runtime_commands, dict):
            for runtime, payload in runtime_commands.items():
                command_set = self._command_set_from_payload(runtime, payload)
                if command_set.commands:
                    self._runtime_commands[command_set.runtime] = command_set

        session_commands = data.get("session_commands")
        if isinstance(session_commands, dict):
            for session_id, payload in session_commands.items():
                if not isinstance(session_id, str) or not session_id:
                    continue
                runtime = payload.get("runtime") if isinstance(payload, dict) else None
                command_set = self._command_set_from_payload(runtime, payload)
                if command_set.commands:
                    self._session_commands[session_id] = SlashCommandSet(
                        runtime=command_set.runtime,
                        commands=command_set.commands,
                        source=command_set.source,
                        updated_at=command_set.updated_at,
                        session_id=session_id,
                    )

    def _command_set_from_payload(
        self,
        runtime: object,
        payload: object,
    ) -> SlashCommandSet:
        runtime_name = _normalize_runtime(runtime if isinstance(runtime, str) else None)
        if not isinstance(payload, dict):
            return self.defaults_for(runtime_name)
        commands: list[SlashCommand] = []
        for item in payload.get("commands", []):
            if not isinstance(item, dict):
                continue
            raw_command = item.get("command")
            if not isinstance(raw_command, str) or not raw_command.strip():
                continue
            raw_description = item.get("description")
            commands.append(
                SlashCommand(
                    _normalize_command(raw_command),
                    raw_description.strip() if isinstance(raw_description, str) else "",
                )
            )
        source = payload.get("source")
        updated_at = payload.get("updated_at")
        return SlashCommandSet(
            runtime=runtime_name,
            commands=commands or list(DEFAULT_COMMANDS[runtime_name]),
            source=source if isinstance(source, str) else "fallback",
            updated_at=updated_at if isinstance(updated_at, (int, float)) else None,
        )

    def _save_cache(self) -> None:
        data = {
            "version": 1,
            "runtime_commands": {
                runtime: command_set.to_response()
                for runtime, command_set in self._runtime_commands.items()
            },
            "session_commands": {
                session_id: command_set.to_response()
                for session_id, command_set in self._session_commands.items()
            },
        }
        try:
            atomic_write_json(self.cache_path, data)
        except OSError as exc:
            logger.warning("failed to persist slash command cache: %s", exc)


slash_command_registry = SlashCommandRegistry()


__all__ = [
    "DEFAULT_COMMANDS",
    "SlashCommand",
    "SlashCommandRegistry",
    "SlashCommandSet",
    "extract_help_stdout_from_transcript",
    "parse_slash_commands_from_help",
    "slash_command_registry",
]
