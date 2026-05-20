"""Codex session management and Telegram topic/window bindings.

This module is the state hub for CodexBot. It keeps topic->window bindings,
resolves tmux windows to Codex session ids, and reads transcript JSONL files for
history/monitoring.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiofiles

from .config import config
from .runtimes import get_runtime
from .slash_commands import slash_command_registry
from .tmux_manager import tmux_manager
from .transcript_parser import PendingToolInfo, TranscriptParser
from .utils import atomic_write_json

logger = logging.getLogger(__name__)

_UUID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")
_SHELL_COMMANDS = {"bash", "fish", "sh", "zsh"}


def _encode_claude_cwd(cwd: str) -> str:
    """Encode an absolute cwd the same way Claude Code names its project dir.

    Claude replaces both `/` and `.` with `-` to derive the directory name
    under `~/.claude/projects/`. For example:
      /Users/mike/Projects/codexbot/.claude/worktrees/foo
        → -Users-mike-Projects-codexbot--claude-worktrees-foo
    """
    return cwd.replace("/", "-").replace(".", "-")


def claude_transcript_path(session_id: str, cwd: str) -> Path | None:
    """Return the expected JSONL path for a Claude Code session.

    Returns ``None`` for empty inputs. The file may not exist yet — callers
    should treat the path as best-effort.
    """
    if not session_id or not cwd:
        return None
    try:
        resolved = Path(cwd).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    encoded = _encode_claude_cwd(str(resolved))
    return config.claude_projects_path / encoded / f"{session_id}.jsonl"


@dataclass
class WindowState:
    """Persistent state for a tmux window."""

    session_id: str = ""
    cwd: str = ""
    window_name: str = ""
    runtime: str = "codex"
    pinned: bool = False

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "session_id": self.session_id,
            "cwd": self.cwd,
            "runtime": self.runtime,
        }
        if self.window_name:
            data["window_name"] = self.window_name
        if self.pinned:
            data["pinned"] = True
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WindowState":
        return cls(
            session_id=data.get("session_id", ""),
            cwd=data.get("cwd", ""),
            window_name=data.get("window_name", ""),
            runtime=data.get("runtime", "codex"),
            pinned=bool(data.get("pinned", False)),
        )


@dataclass
class CodexSession:
    """Information about a Codex session transcript."""

    session_id: str
    summary: str
    message_count: int
    file_path: str


@dataclass
class HistorySnapshot:
    """Parsed transcript history plus metadata for efficient web paging."""

    messages: list[dict[str, Any]]
    total_count: int
    oldest_timestamp: str | None
    newest_timestamp: str | None
    history_version: str


@dataclass
class _HistoryCacheEntry:
    session_id: str
    file_path: str
    size: int
    mtime_ns: int
    messages: list[dict[str, Any]]
    pending_tools: dict[str, PendingToolInfo]

    @property
    def history_version(self) -> str:
        return f"{self.size}:{self.mtime_ns}:{len(self.messages)}"


@dataclass
class SessionManager:
    """Manages bindings and Codex session resolution."""

    window_states: dict[str, WindowState] = field(default_factory=dict)
    user_window_offsets: dict[int, dict[str, int]] = field(default_factory=dict)
    thread_bindings: dict[int, dict[int, str]] = field(default_factory=dict)
    window_display_names: dict[str, str] = field(default_factory=dict)
    group_chat_ids: dict[str, int] = field(default_factory=dict)

    # session_id -> transcript file path
    _session_index: dict[str, Path] = field(
        default_factory=dict, init=False, repr=False
    )
    # session_id -> normalized cwd
    _session_cwd_index: dict[str, str] = field(
        default_factory=dict, init=False, repr=False
    )
    # session_id -> transcript file mtime
    _session_mtime_index: dict[str, float] = field(
        default_factory=dict, init=False, repr=False
    )
    _session_index_loaded_at: float = field(default=0.0, init=False, repr=False)
    # transcript_path -> (mtime, (session_id, cwd)). Old session files don't
    # change, so re-parsing their session_meta header every 2s is wasted work
    # — only ~172 stat() calls per scan after the cache is warm.
    _session_meta_cache: dict[Path, tuple[float, tuple[str, str]]] = field(
        default_factory=dict, init=False, repr=False
    )
    _history_cache: OrderedDict[str, _HistoryCacheEntry] = field(
        default_factory=OrderedDict, init=False, repr=False
    )
    _history_cache_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock, init=False, repr=False
    )
    _force_status_probe_windows: set[str] = field(
        default_factory=set, init=False, repr=False
    )
    _status_probe_last_by_window: dict[str, float] = field(
        default_factory=dict, init=False, repr=False
    )
    _suppress_cwd_fallback_windows: set[str] = field(
        default_factory=set, init=False, repr=False
    )
    _session_rebind_after_by_window: dict[str, float] = field(
        default_factory=dict, init=False, repr=False
    )
    _excluded_session_ids_by_window: dict[str, set[str]] = field(
        default_factory=dict, init=False, repr=False
    )

    def __post_init__(self) -> None:
        self._migrate_legacy_state_if_needed()
        self._load_state()

    def _migrate_legacy_state_if_needed(self) -> None:
        """Copy legacy ~/.ccbot state into ~/.codexbot once."""
        if config.state_file.exists():
            return

        legacy_dir = Path.home() / ".ccbot"
        legacy_state = legacy_dir / "state.json"
        legacy_monitor = legacy_dir / "monitor_state.json"

        if legacy_state.exists():
            config.state_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(legacy_state, config.state_file)
            logger.info("Migrated legacy state file from %s", legacy_state)

        if legacy_monitor.exists() and not config.monitor_state_file.exists():
            config.monitor_state_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(legacy_monitor, config.monitor_state_file)
            logger.info("Migrated legacy monitor state from %s", legacy_monitor)

    def _save_state(self) -> None:
        state: dict[str, Any] = {
            "schema_version": 3,
            "window_states": {k: v.to_dict() for k, v in self.window_states.items()},
            "user_window_offsets": {
                str(uid): offsets for uid, offsets in self.user_window_offsets.items()
            },
            "thread_bindings": {
                str(uid): {str(tid): wid for tid, wid in bindings.items()}
                for uid, bindings in self.thread_bindings.items()
            },
            "window_display_names": self.window_display_names,
            "group_chat_ids": self.group_chat_ids,
        }
        atomic_write_json(config.state_file, state)
        logger.debug("State saved to %s", config.state_file)

    def _is_window_id(self, key: str) -> bool:
        return key.startswith("@") and len(key) > 1 and key[1:].isdigit()

    def _load_state(self) -> None:
        if not config.state_file.exists():
            return

        try:
            state = json.loads(config.state_file.read_text())
            schema_version = state.get("schema_version", 1)
            self.window_states = {
                k: WindowState.from_dict(v)
                for k, v in state.get("window_states", {}).items()
            }
            self.user_window_offsets = {
                int(uid): offsets
                for uid, offsets in state.get("user_window_offsets", {}).items()
            }
            self.thread_bindings = {
                int(uid): {int(tid): wid for tid, wid in bindings.items()}
                for uid, bindings in state.get("thread_bindings", {}).items()
            }
            self.window_display_names = state.get("window_display_names", {})
            self.group_chat_ids = {
                k: int(v) for k, v in state.get("group_chat_ids", {}).items()
            }
            if schema_version < 3:
                # v2 → v3: WindowState gained a `runtime` field. `from_dict`
                # has already defaulted missing entries to "codex"; persist
                # the upgrade so the file no longer carries the legacy
                # top-level `runtime` field.
                logger.info(
                    "Migrating state.json from schema_version=%s to 3", schema_version
                )
                self._save_state()
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.warning("Failed to load state: %s", e)
            self.window_states = {}
            self.user_window_offsets = {}
            self.thread_bindings = {}
            self.window_display_names = {}
            self.group_chat_ids = {}

    async def resolve_stale_ids(self) -> None:
        """Re-resolve stale window ids and drop dead bindings on startup."""
        windows = await tmux_manager.list_windows()
        live_ids = {w.window_id for w in windows}
        by_name = {w.window_name: w.window_id for w in windows}

        changed = False

        new_window_states: dict[str, WindowState] = {}
        for key, ws in self.window_states.items():
            if self._is_window_id(key):
                if key in live_ids:
                    new_window_states[key] = ws
                    continue
                display = self.window_display_names.get(key, ws.window_name or key)
                resolved = by_name.get(display)
                if resolved:
                    new_window_states[resolved] = ws
                    ws.window_name = display
                    self.window_display_names[resolved] = display
                    self.window_display_names.pop(key, None)
                    changed = True
                else:
                    changed = True
                continue

            # old format: key was window name
            resolved = by_name.get(key)
            if resolved:
                ws.window_name = key
                new_window_states[resolved] = ws
                self.window_display_names[resolved] = key
            changed = True

        self.window_states = new_window_states

        for uid, bindings in list(self.thread_bindings.items()):
            new_bindings: dict[int, str] = {}
            for tid, wid in bindings.items():
                if self._is_window_id(wid):
                    if wid in live_ids:
                        new_bindings[tid] = wid
                        continue
                    display = self.window_display_names.get(wid, wid)
                    resolved = by_name.get(display)
                    if resolved:
                        new_bindings[tid] = resolved
                        self.window_display_names[resolved] = display
                        changed = True
                    else:
                        changed = True
                    continue

                resolved = by_name.get(wid)
                if resolved:
                    new_bindings[tid] = resolved
                    self.window_display_names[resolved] = wid
                changed = True

            if new_bindings:
                self.thread_bindings[uid] = new_bindings
            else:
                del self.thread_bindings[uid]

        for uid, offsets in list(self.user_window_offsets.items()):
            new_offsets: dict[str, int] = {}
            for wid, off in offsets.items():
                if self._is_window_id(wid):
                    if wid in live_ids:
                        new_offsets[wid] = off
                        continue
                    display = self.window_display_names.get(wid, wid)
                    resolved = by_name.get(display)
                    if resolved:
                        new_offsets[resolved] = off
                        changed = True
                    else:
                        changed = True
                    continue

                resolved = by_name.get(wid)
                if resolved:
                    new_offsets[resolved] = off
                changed = True
            self.user_window_offsets[uid] = new_offsets

        if changed:
            self._save_state()

    def get_display_name(self, window_id: str) -> str:
        return self.window_display_names.get(window_id, window_id)

    def update_display_name(self, window_id: str, new_name: str) -> None:
        self.window_display_names[window_id] = new_name
        if window_id in self.window_states:
            self.window_states[window_id].window_name = new_name
        self._save_state()

    def set_group_chat_id(
        self, user_id: int, thread_id: int | None, chat_id: int
    ) -> None:
        tid = thread_id or 0
        key = f"{user_id}:{tid}"
        if self.group_chat_ids.get(key) != chat_id:
            self.group_chat_ids[key] = chat_id
            self._save_state()

    def resolve_chat_id(self, user_id: int, thread_id: int | None = None) -> int:
        if thread_id is not None:
            key = f"{user_id}:{thread_id}"
            group_id = self.group_chat_ids.get(key)
            if group_id is not None:
                return group_id
            logger.warning(
                "resolve_chat_id_fallback user_id=%s thread_id=%s fallback_chat_id=%s reason=missing_group_chat_id",
                user_id,
                thread_id,
                user_id,
            )
        return user_id

    def _transcript_mtime(
        self, session_id: str, transcript: Path | None
    ) -> float | None:
        """Return transcript mtime for a session, loading from disk if needed."""
        cached = self._session_mtime_index.get(session_id)
        if cached is not None:
            return cached
        if transcript is None:
            return None
        try:
            return transcript.stat().st_mtime
        except OSError:
            return None

    def _bound_session_ids(self, *, exclude_window_id: str | None = None) -> set[str]:
        """Return session ids already bound to other topics."""
        result: set[str] = set()
        for _user_id, _thread_id, window_id in self.iter_thread_bindings():
            if window_id == exclude_window_id:
                continue
            session_id = self.window_states.get(window_id, WindowState()).session_id
            if session_id:
                result.add(session_id)
        return result

    def _clear_pending_window_rebind(self, window_id: str) -> None:
        self._force_status_probe_windows.discard(window_id)
        self._suppress_cwd_fallback_windows.discard(window_id)
        self._session_rebind_after_by_window.pop(window_id, None)
        self._excluded_session_ids_by_window.pop(window_id, None)

    async def schedule_slash_command_discovery(self, window_id: str) -> None:
        """Schedule one-shot runtime slash-command discovery for a ready window."""
        state = self.get_window_state(window_id)
        if not state.session_id:
            return
        await self._refresh_sessions_index(force=True)
        transcript_path = self._session_index.get(state.session_id)
        slash_command_registry.schedule_discovery(
            runtime=state.runtime,
            window_id=window_id,
            session_id=state.session_id,
            transcript_path=transcript_path,
        )

    def mark_window_for_new_session(
        self,
        window_id: str,
        *,
        clear_existing: bool = False,
    ) -> None:
        """Mark a window as expecting a fresh session transcript.

        This is used after `/clear`, `/new`, and similar session-changing
        commands.  Detection stays filesystem-only and never mutates the Codex TUI.
        """
        state = self.get_window_state(window_id)
        previous_session_id = state.session_id
        self._force_status_probe_windows.add(window_id)
        self._suppress_cwd_fallback_windows.add(window_id)
        self._session_rebind_after_by_window[window_id] = time.time()
        if previous_session_id:
            self._excluded_session_ids_by_window.setdefault(window_id, set()).add(
                previous_session_id
            )
        if clear_existing:
            state.session_id = ""
        self._save_state()

    async def _lookup_session_id_for_cwd(
        self,
        cwd: str,
        *,
        min_mtime: float | None = None,
        exclude_session_ids: set[str] | None = None,
        exclude_window_id: str | None = None,
        force_refresh: bool = True,
    ) -> str | None:
        await self._refresh_sessions_index(force=force_refresh)
        norm = self._normalize_cwd(cwd)
        excluded = set(exclude_session_ids or ())
        excluded.update(self._bound_session_ids(exclude_window_id=exclude_window_id))

        best_sid = None
        best_mtime = -1.0
        for sid, scwd in self._session_cwd_index.items():
            if scwd != norm or sid in excluded:
                continue
            mtime = self._session_mtime_index.get(sid, 0.0)
            if min_mtime is not None and mtime < min_mtime:
                continue
            if mtime > best_mtime:
                best_sid = sid
                best_mtime = mtime
        return best_sid

    async def _fresh_session_id_for_window(
        self,
        window_id: str,
        state: WindowState,
        *,
        force_refresh: bool = True,
    ) -> str | None:
        if not state.cwd:
            return None

        cutoff = self._session_rebind_after_by_window.get(window_id)
        # Filesystems often round mtimes; allow a small tolerance while still
        # excluding the previous session id for this window.
        min_mtime = max(0.0, cutoff - 1.0) if cutoff is not None else None
        excluded = set(self._excluded_session_ids_by_window.get(window_id, set()))
        if state.session_id:
            excluded.add(state.session_id)
        return await self._lookup_session_id_for_cwd(
            state.cwd,
            min_mtime=min_mtime,
            exclude_session_ids=excluded,
            exclude_window_id=window_id,
            force_refresh=force_refresh,
        )

    async def refresh_window_session_if_stale(self, window_id: str) -> str | None:
        """Refresh window->session mapping using transcript discovery only."""
        state = self.get_window_state(window_id)

        if state.runtime == "claude":
            window = await tmux_manager.find_window_by_id(window_id)
            if not window:
                return state.session_id or None
            if not state.cwd:
                state.cwd = self._normalize_cwd(window.cwd)
            if not state.window_name:
                state.window_name = window.window_name

            pane_cmd = (window.pane_current_command or "").lower()
            if pane_cmd in _SHELL_COMMANDS:
                if state.session_id:
                    logger.info(
                        "clearing claude shell window binding window_id=%s session_id=%s",
                        window_id,
                        state.session_id,
                    )
                    state.session_id = ""
                    self._save_state()
                return None

            await self._refresh_sessions_index(force=False)
            transcript = (
                self._session_index.get(state.session_id) if state.session_id else None
            )
            if transcript and not transcript.exists():
                transcript = None
            if state.session_id and transcript:
                return state.session_id

            runtime = get_runtime("claude")
            pane_pid = await tmux_manager.get_pane_pid(window_id)
            fresh_sid = await runtime.discover_session_id(
                window_id=window_id,
                pane_pid=pane_pid,
                cwd=state.cwd,
                allow_cwd_fallback=False,
            )
            if fresh_sid and fresh_sid != state.session_id:
                old_sid = state.session_id
                state.session_id = fresh_sid
                self._save_state()
                logger.info(
                    "window_session_rebound window_id=%s old_session=%s new_session=%s",
                    window_id,
                    old_sid,
                    fresh_sid,
                )
                await self.schedule_slash_command_discovery(window_id)
            elif fresh_sid and not state.session_id:
                state.session_id = fresh_sid
                self._save_state()
                await self.schedule_slash_command_discovery(window_id)

            if state.session_id and state.cwd:
                await self._refresh_sessions_index(force=True)
            return state.session_id or None

        force_refresh = window_id in self._force_status_probe_windows
        if force_refresh:
            self._force_status_probe_windows.discard(window_id)
        suppress_cwd_fallback = window_id in self._suppress_cwd_fallback_windows
        pending_new_session = window_id in self._session_rebind_after_by_window

        if not state.cwd or not state.window_name:
            window = await tmux_manager.find_window_by_id(window_id)
            if not window:
                return state.session_id or None
            if not state.cwd:
                state.cwd = self._normalize_cwd(window.cwd)
            if not state.window_name:
                state.window_name = window.window_name

        if not state.session_id:
            fresh_sid = await self._fresh_session_id_for_window(
                window_id,
                state,
                force_refresh=force_refresh or pending_new_session,
            )
            if fresh_sid:
                state.session_id = fresh_sid
                self._clear_pending_window_rebind(window_id)
                self._save_state()
                await self.schedule_slash_command_discovery(window_id)
                return state.session_id

            if suppress_cwd_fallback:
                return None

            if state.cwd:
                fallback_sid = await self._fallback_session_id_for_cwd(
                    state.cwd,
                    exclude_window_id=window_id,
                )
                if fallback_sid:
                    state.session_id = fallback_sid
                    self._clear_pending_window_rebind(window_id)
                    self._save_state()
                    await self.schedule_slash_command_discovery(window_id)
                    return state.session_id
            return None

        await self._refresh_sessions_index(force=False)
        transcript = self._session_index.get(state.session_id)
        if transcript and not transcript.exists():
            transcript = None

        if transcript and not pending_new_session and not force_refresh:
            return state.session_id

        now = time.monotonic()
        min_interval = max(0.0, config.status_probe_min_interval_seconds)
        last_probe = self._status_probe_last_by_window.get(window_id, 0.0)
        if not force_refresh and now - last_probe < min_interval:
            return state.session_id
        self._status_probe_last_by_window[window_id] = now

        replacement_sid = await self._fresh_session_id_for_window(
            window_id,
            state,
            force_refresh=True,
        )
        if not replacement_sid and transcript is None and not suppress_cwd_fallback:
            replacement_sid = await self._fallback_session_id_for_cwd(
                state.cwd,
                exclude_session_ids={state.session_id},
                exclude_window_id=window_id,
            )

        if replacement_sid and replacement_sid != state.session_id:
            old_sid = state.session_id
            state.session_id = replacement_sid
            self._clear_pending_window_rebind(window_id)
            self._save_state()
            logger.info(
                "window_session_rebound window_id=%s old_session=%s new_session=%s",
                window_id,
                old_sid,
                replacement_sid,
            )
            await self.schedule_slash_command_discovery(window_id)
        elif pending_new_session or transcript is None:
            logger.info(
                "window_session_refresh_no_change window_id=%s session_id=%s pending=%s transcript_missing=%s",
                window_id,
                state.session_id,
                pending_new_session,
                transcript is None,
            )
        return state.session_id or None

    async def _refresh_sessions_index(self, force: bool = False) -> None:
        """Build in-memory index of transcripts by session_id.

        Codex transcripts live under a single root (`config.codex_sessions_path`)
        and are discovered by scanning that tree for JSONL files. Claude Code
        transcripts live under `~/.claude/projects/<encoded-cwd>/<sid>.jsonl`;
        rather than scanning the whole tree (which may be large), we add an
        entry for each currently bound Claude window whose `session_id` is
        already known.
        """
        now = time.monotonic()
        if not force and now - self._session_index_loaded_at < 2.0:
            return

        base = config.codex_sessions_path
        index: dict[str, Path] = {}
        cwd_index: dict[str, str] = {}
        mtime_index: dict[str, float] = {}

        if base.exists():
            seen_paths: set[Path] = set()
            for file_path in base.rglob("*.jsonl"):
                if file_path.name.endswith(".jsonl.bak"):
                    continue
                try:
                    mtime = file_path.stat().st_mtime
                except OSError:
                    continue
                seen_paths.add(file_path)
                cached = self._session_meta_cache.get(file_path)
                if cached and cached[0] == mtime:
                    sid, cwd = cached[1]
                else:
                    sid, cwd = self._read_session_meta(file_path)
                    self._session_meta_cache[file_path] = (mtime, (sid, cwd))
                if not sid:
                    continue
                prev = mtime_index.get(sid)
                if prev is None or mtime >= prev:
                    index[sid] = file_path
                    mtime_index[sid] = mtime
                    cwd_index[sid] = cwd
            # Evict cache entries for files that no longer exist.
            if len(self._session_meta_cache) > len(seen_paths):
                for stale in list(self._session_meta_cache.keys()):
                    if stale not in seen_paths:
                        del self._session_meta_cache[stale]

        # Claude Code: add bound windows whose runtime is "claude".
        for ws in self.window_states.values():
            if ws.runtime != "claude" or not ws.session_id or not ws.cwd:
                continue
            claude_path = claude_transcript_path(ws.session_id, ws.cwd)
            if claude_path is None:
                continue
            try:
                mtime = claude_path.stat().st_mtime
            except OSError:
                # File not yet written; record path anyway so the monitor
                # will pick it up once Claude starts appending.
                mtime = 0.0
            index[ws.session_id] = claude_path
            mtime_index[ws.session_id] = mtime
            cwd_index[ws.session_id] = self._normalize_cwd(ws.cwd)

        self._session_index = index
        self._session_cwd_index = cwd_index
        self._session_mtime_index = mtime_index
        self._session_index_loaded_at = now

    @staticmethod
    def _normalize_cwd(cwd: str) -> str:
        try:
            return str(Path(cwd).expanduser().resolve())
        except (OSError, RuntimeError, ValueError):
            return cwd

    def _read_session_meta(self, file_path: Path) -> tuple[str, str]:
        """Read session id/cwd from transcript meta line."""
        sid = ""
        cwd = ""
        try:
            with file_path.open("r", encoding="utf-8") as f:
                for idx, line in enumerate(f):
                    if idx > 30:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if data.get("type") != "session_meta":
                        continue
                    payload = data.get("payload", {})
                    if not isinstance(payload, dict):
                        continue
                    sid = payload.get("id", "")
                    cwd = payload.get("cwd", "")
                    break
        except OSError:
            return "", ""

        if not sid:
            stem = file_path.stem
            if _UUID_RE.match(stem):
                sid = stem

        return sid, self._normalize_cwd(cwd)

    async def _fallback_session_id_for_cwd(
        self,
        cwd: str,
        *,
        exclude_session_ids: set[str] | None = None,
        exclude_window_id: str | None = None,
    ) -> str | None:
        return await self._lookup_session_id_for_cwd(
            cwd,
            exclude_session_ids=exclude_session_ids,
            exclude_window_id=exclude_window_id,
            force_refresh=True,
        )

    async def get_session_file_path(self, session_id: str) -> Path | None:
        """Return transcript file path for a session id, if known."""
        await self._refresh_sessions_index(force=True)
        file_path = self._session_index.get(session_id)
        if file_path and file_path.exists():
            return file_path
        return None

    async def wait_for_session_map_entry(
        self, window_id: str, timeout: float = 5.0, interval: float = 0.5
    ) -> bool:
        """Compatibility method: detect a transcript-backed session id for a window."""
        ws = self.get_window_state(window_id)
        if not ws.cwd:
            w = await tmux_manager.find_window_by_id(window_id)
            if w:
                ws.cwd = self._normalize_cwd(w.cwd)
                ws.window_name = w.window_name

        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            sid = await self.refresh_window_session_if_stale(window_id)
            if sid and ws.session_id:
                return True
            await asyncio.sleep(interval)
        return False

    async def load_session_map(self) -> None:
        """Compatibility no-op for legacy session discovery API.

        Probes unassigned windows and attempts to detect their Codex session ids.
        """
        windows = await tmux_manager.list_windows()
        for w in windows:
            ws = self.get_window_state(w.window_id)
            if not ws.window_name:
                ws.window_name = w.window_name
            if not ws.cwd:
                ws.cwd = self._normalize_cwd(w.cwd)
            if ws.session_id:
                continue
            # The cwd-based fallback scans `~/.codex/sessions` and is only
            # meaningful for codex windows. Assigning a codex session id to a
            # claude window churns: refresh_window_session_if_stale clears it
            # immediately and we log "clearing claude shell window binding"
            # forever. Claude session ids are captured at window creation.
            if ws.runtime == "claude":
                continue
            fallback_sid = await self._fallback_session_id_for_cwd(ws.cwd)
            if fallback_sid:
                ws.session_id = fallback_sid
                self._save_state()
                continue
            await self.wait_for_session_map_entry(
                w.window_id,
                timeout=min(config.session_detect_timeout, 2.0),
                interval=config.session_detect_interval,
            )

    def get_window_state(self, window_id: str) -> WindowState:
        if window_id not in self.window_states:
            self.window_states[window_id] = WindowState()
        return self.window_states[window_id]

    def clear_window_session(self, window_id: str) -> None:
        self.mark_window_for_new_session(window_id, clear_existing=True)

    async def _get_session_direct(
        self, session_id: str, cwd: str | None = None
    ) -> CodexSession | None:
        """Get session metadata directly from transcript file."""
        await self._refresh_sessions_index(force=True)
        file_path = self._session_index.get(session_id)
        if not file_path or not file_path.exists():
            return None

        summary = ""
        last_user = ""
        message_count = 0

        try:
            async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
                async for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    message_count += 1
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    parsed = TranscriptParser.parse_message(data)
                    if not parsed or not parsed.text.strip():
                        continue
                    text = parsed.text.strip()
                    if parsed.message_type == "assistant":
                        summary = text
                    elif parsed.message_type == "user":
                        last_user = text
        except OSError:
            return None

        if not summary:
            summary = (last_user or "Untitled")[:80]

        return CodexSession(
            session_id=session_id,
            summary=summary,
            message_count=message_count,
            file_path=str(file_path),
        )

    async def list_sessions_for_directory(self, cwd: str) -> list[CodexSession]:
        """List existing Codex sessions for a directory."""
        await self._refresh_sessions_index(force=True)
        norm_cwd = self._normalize_cwd(cwd)

        session_ids = [
            sid for sid, scwd in self._session_cwd_index.items() if scwd == norm_cwd
        ]
        session_ids.sort(
            key=lambda sid: self._session_mtime_index.get(sid, 0.0), reverse=True
        )

        sessions: list[CodexSession] = []
        for sid in session_ids[:10]:
            s = await self._get_session_direct(sid, cwd=norm_cwd)
            if s and s.message_count > 0:
                sessions.append(s)
        return sessions

    async def resolve_session_for_window(self, window_id: str) -> CodexSession | None:
        """Resolve a tmux window to a transcript-backed session record."""
        await self.refresh_window_session_if_stale(window_id)
        state = self.get_window_state(window_id)

        if not state.cwd:
            window = await tmux_manager.find_window_by_id(window_id)
            if window:
                state.cwd = self._normalize_cwd(window.cwd)
                state.window_name = window.window_name

        if state.runtime == "claude":
            # Claude session ids are captured at creation; no fallback probing.
            if not state.session_id:
                return None
            return await self._get_session_direct(state.session_id, state.cwd)

        if not state.session_id:
            suppress_cwd_fallback = window_id in self._suppress_cwd_fallback_windows
            if state.cwd and not suppress_cwd_fallback:
                fallback_sid = await self._fallback_session_id_for_cwd(state.cwd)
                if fallback_sid:
                    state.session_id = fallback_sid
                    self._save_state()
            if not state.session_id:
                detected = await self.wait_for_session_map_entry(
                    window_id,
                    timeout=min(config.session_detect_timeout, 3.0),
                    interval=config.session_detect_interval,
                )
                if not detected:
                    return None

        session = await self._get_session_direct(state.session_id, state.cwd)
        if session:
            return session

        # The stored session id could be stale after /new; clear and retry once.
        old_sid = state.session_id
        state.session_id = ""
        self._save_state()

        detected = await self.wait_for_session_map_entry(
            window_id,
            timeout=min(config.session_detect_timeout, 3.0),
            interval=config.session_detect_interval,
        )
        if not detected:
            logger.warning(
                "No Codex session for window %s (stale sid=%s)", window_id, old_sid
            )
            return None

        return await self._get_session_direct(state.session_id, state.cwd)

    def update_user_window_offset(
        self, user_id: int, window_id: str, offset: int
    ) -> None:
        if user_id not in self.user_window_offsets:
            self.user_window_offsets[user_id] = {}
        self.user_window_offsets[user_id][window_id] = offset
        self._save_state()

    def bind_thread(
        self, user_id: int, thread_id: int, window_id: str, window_name: str = ""
    ) -> None:
        if user_id not in self.thread_bindings:
            self.thread_bindings[user_id] = {}
        self.thread_bindings[user_id][thread_id] = window_id
        if window_name:
            self.window_display_names[window_id] = window_name
            ws = self.get_window_state(window_id)
            ws.window_name = window_name
        self._save_state()

    def unbind_thread(self, user_id: int, thread_id: int) -> str | None:
        bindings = self.thread_bindings.get(user_id)
        if not bindings or thread_id not in bindings:
            return None
        wid = bindings.pop(thread_id)
        if not bindings:
            del self.thread_bindings[user_id]
        self._save_state()
        return wid

    def get_window_for_thread(self, user_id: int, thread_id: int) -> str | None:
        bindings = self.thread_bindings.get(user_id)
        if not bindings:
            return None
        return bindings.get(thread_id)

    def is_window_bound_to_thread(
        self, user_id: int, thread_id: int | None, window_id: str
    ) -> bool:
        """Return whether `window_id` is currently bound to `thread_id`."""
        if thread_id is None:
            return False
        return self.get_window_for_thread(user_id, thread_id) == window_id

    def resolve_window_for_thread(
        self, user_id: int, thread_id: int | None
    ) -> str | None:
        if thread_id is None:
            return None
        return self.get_window_for_thread(user_id, thread_id)

    def iter_thread_bindings(self) -> Iterator[tuple[int, int, str]]:
        for user_id, bindings in self.thread_bindings.items():
            for thread_id, window_id in bindings.items():
                yield user_id, thread_id, window_id

    async def find_users_for_session(
        self, session_id: str
    ) -> list[tuple[int, str, int]]:
        result: list[tuple[int, str, int]] = []
        for user_id, thread_id, window_id in self.iter_thread_bindings():
            resolved = await self.resolve_session_for_window(window_id)
            if resolved and resolved.session_id == session_id:
                result.append((user_id, window_id, thread_id))
        return result

    async def send_to_window(self, window_id: str, text: str) -> tuple[bool, str]:
        display = self.get_display_name(window_id)
        window = await tmux_manager.find_window_by_id(window_id)
        if not window:
            return False, "Window not found (may have been closed)"
        success = await tmux_manager.send_keys(window.window_id, text)
        if success:
            command = text.strip().split(maxsplit=1)[0].lower() if text.strip() else ""
            if command in {"/clear", "/new"}:
                self.clear_window_session(window_id)
            elif command == "/resume":
                self.mark_window_for_new_session(window_id, clear_existing=False)
            elif (
                not self.get_window_state(window_id).session_id
                and window_id in self._suppress_cwd_fallback_windows
            ):
                # The first post-/clear message is what creates the new session.
                self.mark_window_for_new_session(window_id, clear_existing=False)
            return True, f"Sent to {display}"
        return False, "Failed to send keys"

    async def get_recent_messages(
        self,
        window_id: str,
        *,
        start_byte: int = 0,
        end_byte: int | None = None,
    ) -> tuple[list[dict], int]:
        if start_byte == 0 and end_byte is None:
            snapshot = await self.get_history_snapshot(window_id)
            return snapshot.messages, snapshot.total_count

        session = await self.resolve_session_for_window(window_id)
        if not session or not session.file_path:
            return [], 0

        file_path = Path(session.file_path)
        if not file_path.exists():
            return [], 0

        try:
            entries = await self._read_transcript_entries(
                file_path, start_byte=start_byte, end_byte=end_byte
            )
        except OSError as e:
            logger.error("Error reading session file %s: %s", file_path, e)
            return [], 0

        parsed_entries, _ = TranscriptParser.parse_entries(entries)
        messages = _messages_from_parsed(parsed_entries)
        return messages, len(messages)

    async def get_history_snapshot(self, window_id: str) -> HistorySnapshot:
        """Return cached parsed transcript history for a window."""
        session = await self.resolve_session_for_window(window_id)
        if not session or not session.file_path:
            return HistorySnapshot([], 0, None, None, "")

        file_path = Path(session.file_path)
        if not file_path.exists():
            return HistorySnapshot([], 0, None, None, "")

        try:
            entry = await self._get_history_cache_entry(session, file_path)
        except OSError as e:
            logger.error("Error reading session file %s: %s", file_path, e)
            return HistorySnapshot([], 0, None, None, "")

        messages = list(entry.messages)
        return HistorySnapshot(
            messages=messages,
            total_count=len(messages),
            oldest_timestamp=_first_timestamp(messages),
            newest_timestamp=_last_timestamp(messages),
            history_version=entry.history_version,
        )

    async def _get_history_cache_entry(
        self, session: CodexSession, file_path: Path
    ) -> _HistoryCacheEntry:
        cache_key = f"{session.session_id}:{file_path}"
        stat = file_path.stat()
        async with self._history_cache_lock:
            cached = self._history_cache.get(cache_key)
            if (
                cached is not None
                and cached.size == stat.st_size
                and cached.mtime_ns == stat.st_mtime_ns
            ):
                self._touch_history_cache(cache_key)
                return cached

            if (
                cached is not None
                and not cached.pending_tools
                and cached.size < stat.st_size
                and cached.file_path == str(file_path)
                and cached.session_id == session.session_id
            ):
                appended = await self._read_transcript_entries(
                    file_path, start_byte=cached.size, end_byte=stat.st_size
                )
                if appended:
                    parsed_entries, pending_tools = TranscriptParser.parse_entries(
                        appended, pending_tools={}
                    )
                    cached.messages.extend(_messages_from_parsed(parsed_entries))
                    cached.pending_tools = pending_tools
                cached.size = stat.st_size
                cached.mtime_ns = stat.st_mtime_ns
                self._touch_history_cache(cache_key)
                return cached

            entries = await self._read_transcript_entries(file_path)
            parsed_entries, pending_tools = TranscriptParser.parse_entries(entries)
            rebuilt = _HistoryCacheEntry(
                session_id=session.session_id,
                file_path=str(file_path),
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                messages=_messages_from_parsed(parsed_entries),
                pending_tools=pending_tools,
            )
            self._history_cache[cache_key] = rebuilt
            self._touch_history_cache(cache_key)
            self._trim_history_cache()
            return rebuilt

    async def _read_transcript_entries(
        self,
        file_path: Path,
        *,
        start_byte: int = 0,
        end_byte: int | None = None,
    ) -> list[dict]:
        entries: list[dict] = []
        async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
            if start_byte > 0:
                await f.seek(start_byte)

            while True:
                if end_byte is not None:
                    cur = await f.tell()
                    if cur >= end_byte:
                        break
                    line_offset = cur
                else:
                    line_offset = await f.tell()

                line = await f.readline()
                if not line:
                    break

                data = TranscriptParser.parse_line(line)
                if data:
                    data[TranscriptParser.TRANSCRIPT_OFFSET_KEY] = line_offset
                    entries.append(data)
        return entries

    def _touch_history_cache(self, cache_key: str) -> None:
        if cache_key in self._history_cache:
            self._history_cache.move_to_end(cache_key)

    def _trim_history_cache(self) -> None:
        max_entries = max(1, config.history_cache_max_sessions)
        while len(self._history_cache) > max_entries:
            self._history_cache.popitem(last=False)


def _messages_from_parsed(parsed_entries: list[Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for e in parsed_entries:
        message: dict[str, Any] = {
            "role": e.role,
            "text": e.text,
            "content_type": e.content_type,
            "timestamp": e.timestamp,
        }
        if getattr(e, "tool_name", None) is not None:
            message["tool_name"] = e.tool_name
        if getattr(e, "tool_input", None) is not None:
            message["tool_input"] = e.tool_input
        if getattr(e, "tool_use_id", None) is not None:
            message["tool_use_id"] = e.tool_use_id
        if getattr(e, "transcript_offset", None) is not None:
            message["transcript_offset"] = e.transcript_offset
        if getattr(e, "transcript_index", None) is not None:
            message["transcript_index"] = e.transcript_index
        messages.append(message)
    return messages


def _first_timestamp(messages: list[dict[str, Any]]) -> str | None:
    for message in messages:
        timestamp = message.get("timestamp")
        if isinstance(timestamp, str) and timestamp:
            return timestamp
    return None


def _last_timestamp(messages: list[dict[str, Any]]) -> str | None:
    for message in reversed(messages):
        timestamp = message.get("timestamp")
        if isinstance(timestamp, str) and timestamp:
            return timestamp
    return None


session_manager = SessionManager()
