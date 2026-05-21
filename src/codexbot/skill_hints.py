"""Codex skill hint discovery and caching for the Web UI composer."""

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
from .skills import discover_skills
from .utils import atomic_write_json

logger = logging.getLogger(__name__)

EventPublisher = Callable[[dict[str, Any]], Awaitable[None]]

_SKILL_LINE_RE = re.compile(
    r"^\s*(?:[-*]|\d+[.)])\s+`?\$?(?P<name>[a-z][a-z0-9_.-]*)`?\s*:\s*(?P<description>.*)$"
)
_FILE_SUFFIX_RE = re.compile(r"\s*\(file:\s*[^)]*\)\s*$")


@dataclass(frozen=True)
class SkillHint:
    name: str
    invocation: str
    description: str = ""


@dataclass(frozen=True)
class SkillHintSet:
    runtime: str
    skills: list[SkillHint]
    source: str
    updated_at: float | None = None
    window_id: str | None = None
    session_id: str | None = None

    def to_response(self) -> dict[str, Any]:
        return {
            "runtime": self.runtime,
            "window_id": self.window_id,
            "session_id": self.session_id,
            "skills": [asdict(skill) for skill in self.skills],
            "source": self.source,
            "updated_at": self.updated_at,
        }


def _normalize_runtime(runtime: str | None) -> str:
    return "claude" if (runtime or "").strip().lower() == "claude" else "codex"


def _clean_description(raw: str) -> str:
    text = _FILE_SUFFIX_RE.sub("", raw).strip()
    return re.sub(r"\s+", " ", text)


def _hint_from_name(name: str, description: str = "") -> SkillHint:
    clean_name = name.strip().strip("`").lstrip("$")
    return SkillHint(
        name=clean_name,
        invocation=f"${clean_name}",
        description=_clean_description(description),
    )


def parse_skill_hints_from_instructions(instructions: str) -> list[SkillHint]:
    """Extract Codex skill hints from session instructions."""
    hints: list[SkillHint] = []
    seen: set[str] = set()
    for raw_line in (instructions or "").splitlines():
        match = _SKILL_LINE_RE.match(raw_line)
        if not match:
            continue
        name = match.group("name").strip()
        if name in seen:
            continue
        seen.add(name)
        hints.append(_hint_from_name(name, match.group("description")))
    return hints


def extract_skill_hints_from_transcript(transcript_path: Path) -> list[SkillHint]:
    """Return skill hints from the transcript's session metadata."""
    if not transcript_path.exists():
        return []
    try:
        with transcript_path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if data.get("type") != "session_meta":
                    continue
                payload = data.get("payload")
                if not isinstance(payload, dict):
                    continue
                instructions = payload.get("instructions")
                if not isinstance(instructions, str):
                    continue
                hints = parse_skill_hints_from_instructions(instructions)
                if hints:
                    return hints
    except OSError as exc:
        logger.debug(
            "failed to read skill-hint transcript %s: %s", transcript_path, exc
        )
    return []


class SkillHintRegistry:
    def __init__(self, *, cache_path: Path | None = None) -> None:
        self.cache_path = cache_path or (config.config_dir / "skill_hints.json")
        self._runtime_hints: dict[str, SkillHintSet] = {}
        self._session_hints: dict[str, SkillHintSet] = {}
        self._inflight: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._event_publisher: EventPublisher | None = None
        self._load_cache()

    def set_event_publisher(self, publisher: EventPublisher | None) -> None:
        self._event_publisher = publisher

    def has_session_hints(self, session_id: str | None) -> bool:
        return bool(session_id and session_id in self._session_hints)

    def defaults_for(self, runtime: str | None) -> SkillHintSet:
        runtime_name = _normalize_runtime(runtime)
        if runtime_name != "codex":
            return SkillHintSet(runtime=runtime_name, skills=[], source="unsupported")
        skills = [_hint_from_name(name) for name in discover_skills("codex")]
        return SkillHintSet(runtime=runtime_name, skills=skills, source="filesystem")

    def get_hints(
        self,
        runtime: str | None,
        *,
        window_id: str | None = None,
        session_id: str | None = None,
    ) -> SkillHintSet:
        runtime_name = _normalize_runtime(runtime)
        if runtime_name != "codex":
            return SkillHintSet(
                runtime=runtime_name,
                skills=[],
                source="unsupported",
                window_id=window_id,
                session_id=session_id,
            )
        if session_id and session_id in self._session_hints:
            hint_set = self._session_hints[session_id]
        else:
            hint_set = self._runtime_hints.get(runtime_name) or self.defaults_for(
                runtime_name
            )
        return SkillHintSet(
            runtime=runtime_name,
            skills=list(hint_set.skills),
            source=hint_set.source,
            updated_at=hint_set.updated_at,
            window_id=window_id,
            session_id=session_id,
        )

    async def discover_now(
        self,
        *,
        runtime: str | None,
        window_id: str,
        session_id: str,
        transcript_path: Path | str | None,
        publish: bool = True,
    ) -> SkillHintSet:
        runtime_name = _normalize_runtime(runtime)
        if runtime_name != "codex":
            return self.get_hints(
                runtime_name,
                window_id=window_id,
                session_id=session_id,
            )

        hints: list[SkillHint] = []
        if transcript_path is not None:
            hints = await asyncio.to_thread(
                extract_skill_hints_from_transcript,
                Path(transcript_path),
            )
        source = "transcript" if hints else "filesystem"
        if not hints:
            hints = [_hint_from_name(name) for name in discover_skills("codex")]
        hint_set = self._remember(
            runtime_name,
            window_id,
            session_id,
            hints,
            source,
            publish=publish,
        )
        return hint_set

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
        if runtime_name != "codex" or not window_id or not session_id:
            return
        if not force and session_id in self._session_hints:
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
                transcript_path=Path(transcript_path)
                if transcript_path is not None
                else None,
            ),
            name=f"skill-hint-discovery:{runtime_name}:{window_id}",
        )

    async def _run_discovery(
        self,
        *,
        runtime: str,
        window_id: str,
        session_id: str,
        transcript_path: Path | None,
    ) -> None:
        key = (runtime, session_id)
        try:
            await self.discover_now(
                runtime=runtime,
                window_id=window_id,
                session_id=session_id,
                transcript_path=transcript_path,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "skill_hint_discovery_failed runtime=%s window=%s session=%s: %s",
                runtime,
                window_id,
                session_id,
                exc,
            )
        finally:
            self._inflight.pop(key, None)

    def _remember(
        self,
        runtime: str,
        window_id: str,
        session_id: str,
        hints: list[SkillHint],
        source: str,
        *,
        publish: bool,
    ) -> SkillHintSet:
        now = time.time()
        hint_set = SkillHintSet(
            runtime=runtime,
            skills=hints,
            source=source,
            updated_at=now,
            window_id=window_id,
            session_id=session_id,
        )
        self._runtime_hints[runtime] = SkillHintSet(
            runtime=runtime,
            skills=hints,
            source=source,
            updated_at=now,
        )
        self._session_hints[session_id] = hint_set
        self._save_cache()
        if publish:
            self._publish_changed(runtime, window_id, session_id, source)
        return hint_set

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
            "type": "skill_hints_changed",
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
            logger.debug("no running loop for skill hint event")
            return
        loop.create_task(_publish(), name="skill-hint-event")

    def _load_cache(self) -> None:
        try:
            data = json.loads(self.cache_path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return

        runtime_hints = data.get("runtime_hints")
        if isinstance(runtime_hints, dict):
            for runtime, payload in runtime_hints.items():
                hint_set = self._hint_set_from_payload(runtime, payload)
                if hint_set.skills:
                    self._runtime_hints[hint_set.runtime] = hint_set

        session_hints = data.get("session_hints")
        if isinstance(session_hints, dict):
            for session_id, payload in session_hints.items():
                if not isinstance(session_id, str) or not session_id:
                    continue
                runtime = payload.get("runtime") if isinstance(payload, dict) else None
                hint_set = self._hint_set_from_payload(runtime, payload)
                if hint_set.skills:
                    self._session_hints[session_id] = SkillHintSet(
                        runtime=hint_set.runtime,
                        skills=hint_set.skills,
                        source=hint_set.source,
                        updated_at=hint_set.updated_at,
                        session_id=session_id,
                    )

    def _hint_set_from_payload(self, runtime: object, payload: object) -> SkillHintSet:
        runtime_name = _normalize_runtime(runtime if isinstance(runtime, str) else None)
        if not isinstance(payload, dict):
            return self.defaults_for(runtime_name)
        skills: list[SkillHint] = []
        for item in payload.get("skills", []):
            if not isinstance(item, dict):
                continue
            raw_name = item.get("name")
            if not isinstance(raw_name, str) or not raw_name.strip():
                continue
            raw_description = item.get("description")
            raw_invocation = item.get("invocation")
            name = raw_name.strip().strip("`").lstrip("$")
            invocation = (
                raw_invocation.strip()
                if isinstance(raw_invocation, str) and raw_invocation.strip()
                else f"${name}"
            )
            skills.append(
                SkillHint(
                    name=name,
                    invocation=invocation,
                    description=raw_description.strip()
                    if isinstance(raw_description, str)
                    else "",
                )
            )
        source = payload.get("source")
        updated_at = payload.get("updated_at")
        return SkillHintSet(
            runtime=runtime_name,
            skills=skills,
            source=source if isinstance(source, str) else "filesystem",
            updated_at=updated_at if isinstance(updated_at, (int, float)) else None,
        )

    def _save_cache(self) -> None:
        data = {
            "version": 1,
            "runtime_hints": {
                runtime: hint_set.to_response()
                for runtime, hint_set in self._runtime_hints.items()
            },
            "session_hints": {
                session_id: hint_set.to_response()
                for session_id, hint_set in self._session_hints.items()
            },
        }
        try:
            atomic_write_json(self.cache_path, data)
        except OSError as exc:
            logger.warning("failed to persist skill hint cache: %s", exc)


skill_hint_registry = SkillHintRegistry()


__all__ = [
    "SkillHint",
    "SkillHintRegistry",
    "SkillHintSet",
    "extract_skill_hints_from_transcript",
    "parse_skill_hints_from_instructions",
    "skill_hint_registry",
]
