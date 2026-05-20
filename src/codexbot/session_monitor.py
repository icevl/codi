"""Session monitoring service — watches Codex transcript JSONL files.

Runs an async polling loop that:
  1. Resolves active sessions from current topic->window bindings.
  2. Detects binding/session changes and cleans up stale tracked sessions.
  3. Reads new JSONL lines using byte-offset tracking.
  4. Parses entries via TranscriptParser and emits NewMessage objects.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

import aiofiles

from .config import config
from .monitor_state import MonitorState, TrackedSession
from .transcript_parser import TranscriptParser

logger = logging.getLogger(__name__)

# Last successful session monitor loop heartbeat (monotonic time).
_monitor_heartbeat: float = 0.0
_PARTIAL_JSONL_WARN_RETRY_LIMIT = 3


def get_session_monitor_heartbeat_age() -> float | None:
    """Return seconds since session monitor heartbeat, or None if never started."""
    if _monitor_heartbeat <= 0.0:
        return None
    return max(0.0, time.monotonic() - _monitor_heartbeat)


@dataclass
class SessionInfo:
    """Information about a Codex session."""

    session_id: str
    file_path: Path


@dataclass
class NewMessage:
    """A new message detected by the monitor."""

    session_id: str
    text: str
    is_complete: bool
    message_type: Literal["content", "completion"] = "content"
    turn_id: int | None = None
    is_stale_turn: bool = False
    turn_had_visible_output: bool = False
    content_type: str = "text"
    tool_use_id: str | None = None
    role: str = "assistant"
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    image_data: list[tuple[str, bytes]] | None = None
    timestamp: str | None = None


@dataclass
class _TurnState:
    """Per-session turn tracking for explicit completion events."""

    active_turn_id: int | None = None
    next_turn_id: int = 1
    active_turn_had_visible_output: bool = False
    pending_completion_turn_id: int | None = None
    pending_completion_had_visible_output: bool = False
    emitted_completion_turns: set[int] = field(default_factory=set)


@dataclass
class _PartialLineState:
    """Per-session suppression state for repeated partial JSONL reads."""

    offset: int
    retries: int = 0


class SessionMonitor:
    """Monitors Codex sessions for new transcript messages."""

    def __init__(
        self,
        projects_path: Path | None = None,
        poll_interval: float | None = None,
        state_file: Path | None = None,
    ):
        # Keep "projects_path" arg name for compatibility with existing tests.
        self.sessions_path = (
            projects_path if projects_path is not None else config.codex_sessions_path
        )
        self.poll_interval = (
            poll_interval if poll_interval is not None else config.monitor_poll_interval
        )

        self.state = MonitorState(state_file=state_file or config.monitor_state_file)
        self.state.load()

        self._running = False
        self._task: asyncio.Task | None = None
        self._message_callback: Callable[[NewMessage], Awaitable[None]] | None = None
        self._extra_listeners: list[Callable[[NewMessage], Awaitable[None]]] = []
        self._pending_tools: dict[str, dict[str, Any]] = {}
        self._pending_initial_offsets: dict[str, int] = {}
        self._last_window_sessions: dict[str, str] = {}
        self._file_mtimes: dict[str, float] = {}
        self._turn_state: dict[str, _TurnState] = {}
        self._partial_line_state: dict[str, _PartialLineState] = {}

    def _get_turn_state(self, session_id: str) -> _TurnState:
        return self._turn_state.setdefault(session_id, _TurnState())

    def _clear_partial_line_state(self, session_id: str) -> None:
        self._partial_line_state.pop(session_id, None)

    def _record_partial_line(self, session_id: str, offset: int) -> None:
        state = self._partial_line_state.get(session_id)
        if state is None or state.offset != offset:
            state = _PartialLineState(offset=offset, retries=1)
            self._partial_line_state[session_id] = state
        else:
            state.retries += 1

        if state.retries <= _PARTIAL_JSONL_WARN_RETRY_LIMIT:
            logger.warning(
                "Partial JSONL line in session %s, retrying next cycle",
                session_id,
            )
            return

        if state.retries == _PARTIAL_JSONL_WARN_RETRY_LIMIT + 1:
            logger.info(
                "Partial JSONL line in session %s persisted at offset %d; "
                "suppressing further warnings until transcript advances",
                session_id,
                offset,
            )

    def _begin_turn(self, turn_state: _TurnState) -> int:
        turn_state.active_turn_id = turn_state.next_turn_id
        turn_state.next_turn_id += 1
        turn_state.active_turn_had_visible_output = False
        return turn_state.active_turn_id

    def _build_completion_message(
        self,
        session_id: str,
        turn_state: _TurnState,
        turn_id: int,
        is_stale: bool,
        turn_had_visible_output: bool,
    ) -> NewMessage | None:
        if turn_id in turn_state.emitted_completion_turns:
            return None

        turn_state.emitted_completion_turns.add(turn_id)
        return NewMessage(
            session_id=session_id,
            text="",
            is_complete=True,
            message_type="completion",
            turn_id=turn_id,
            is_stale_turn=is_stale,
            turn_had_visible_output=turn_had_visible_output,
            content_type="completion",
            role="assistant",
        )

    def _queue_completion(
        self,
        turn_state: _TurnState,
        turn_id: int,
        *,
        turn_had_visible_output: bool,
    ) -> None:
        """Queue a completion marker for later emission."""
        if turn_id in turn_state.emitted_completion_turns:
            return
        if turn_state.pending_completion_turn_id == turn_id:
            turn_state.pending_completion_had_visible_output = (
                turn_state.pending_completion_had_visible_output
                or turn_had_visible_output
            )
            return
        turn_state.pending_completion_turn_id = turn_id
        turn_state.pending_completion_had_visible_output = turn_had_visible_output

    def _emit_pending_completion(
        self,
        session_id: str,
        turn_state: _TurnState,
        *,
        is_stale: bool,
    ) -> NewMessage | None:
        """Emit and clear a pending completion marker if present."""
        turn_id = turn_state.pending_completion_turn_id
        if turn_id is None:
            return None

        had_visible_output = turn_state.pending_completion_had_visible_output
        message = self._build_completion_message(
            session_id,
            turn_state,
            turn_id,
            is_stale=is_stale,
            turn_had_visible_output=had_visible_output,
        )

        turn_state.pending_completion_turn_id = None
        turn_state.pending_completion_had_visible_output = False
        return message

    def set_message_callback(
        self, callback: Callable[[NewMessage], Awaitable[None]]
    ) -> None:
        self._message_callback = callback

    def add_listener(self, callback: Callable[[NewMessage], Awaitable[None]]) -> None:
        """Register an extra async listener fanned out alongside the primary callback."""
        self._extra_listeners.append(callback)

    def remove_listener(
        self, callback: Callable[[NewMessage], Awaitable[None]]
    ) -> None:
        try:
            self._extra_listeners.remove(callback)
        except ValueError:
            pass

    def set_initial_offset(self, session_id: str, offset: int) -> None:
        """Start monitoring a session from a specific byte offset."""
        safe_offset = max(0, offset)
        tracked = self.state.get_session(session_id)
        if tracked is not None:
            if safe_offset > tracked.last_byte_offset:
                tracked.last_byte_offset = safe_offset
                self.state.update_session(tracked)
                self.state.save_if_dirty()
            self._pending_initial_offsets.pop(session_id, None)
            return

        previous = self._pending_initial_offsets.get(session_id, 0)
        self._pending_initial_offsets[session_id] = max(previous, safe_offset)

    async def _resolve_active_sessions(
        self, active_session_ids: set[str]
    ) -> list[SessionInfo]:
        """Resolve active session IDs to transcript files."""
        if not active_session_ids:
            return []

        from .session import session_manager

        sessions: list[SessionInfo] = []
        for session_id in active_session_ids:
            file_path = await session_manager.get_session_file_path(session_id)
            if file_path:
                sessions.append(SessionInfo(session_id=session_id, file_path=file_path))
        return sessions

    async def _read_new_lines(
        self, session: TrackedSession, file_path: Path
    ) -> list[dict]:
        """Read new JSONL lines from file_path using byte offsets."""
        new_entries: list[dict] = []
        try:
            async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
                await f.seek(0, 2)
                file_size = await f.tell()

                if session.last_byte_offset > file_size:
                    logger.info(
                        "File truncated for session %s (offset %d > size %d), resetting",
                        session.session_id,
                        session.last_byte_offset,
                        file_size,
                    )
                    session.last_byte_offset = 0
                    self._clear_partial_line_state(session.session_id)

                await f.seek(session.last_byte_offset)

                if session.last_byte_offset > 0:
                    first_char = await f.read(1)
                    if first_char and first_char != "{":
                        logger.warning(
                            "Corrupted offset %d in session %s (mid-line), recovering",
                            session.last_byte_offset,
                            session.session_id,
                        )
                        await f.readline()
                        session.last_byte_offset = await f.tell()
                        self._clear_partial_line_state(session.session_id)
                        return []
                    await f.seek(session.last_byte_offset)

                safe_offset = session.last_byte_offset
                partial_line_seen = False
                async for line in f:
                    data = TranscriptParser.parse_line(line)
                    if data:
                        new_entries.append(data)
                        safe_offset = await f.tell()
                    elif line.strip():
                        self._record_partial_line(session.session_id, safe_offset)
                        partial_line_seen = True
                        break
                    else:
                        safe_offset = await f.tell()

                if not partial_line_seen:
                    self._clear_partial_line_state(session.session_id)
                session.last_byte_offset = safe_offset
        except OSError as e:
            logger.error("Error reading session file %s: %s", file_path, e)
        return new_entries

    async def check_for_updates(
        self,
        active_session_ids: set[str],
        *,
        bootstrap: bool = True,
    ) -> list[NewMessage]:
        """Check active sessions for new transcript messages."""
        new_messages: list[NewMessage] = []
        sessions = await self._resolve_active_sessions(active_session_ids)

        for session_info in sessions:
            try:
                tracked = self.state.get_session(session_info.session_id)
                try:
                    st = session_info.file_path.stat()
                    current_mtime = st.st_mtime
                    current_size = st.st_size
                except OSError:
                    continue

                if tracked is None:
                    initial_offset = self._pending_initial_offsets.pop(
                        session_info.session_id, None
                    )
                    if initial_offset is not None:
                        start_offset = min(initial_offset, current_size)
                        logger.info(
                            "Started tracking session: %s (initial offset=%d size=%d)",
                            session_info.session_id,
                            start_offset,
                            current_size,
                        )
                    elif bootstrap:
                        start_offset = current_size
                        logger.info(
                            "Started tracking session: %s (bootstrap offset=%d)",
                            session_info.session_id,
                            start_offset,
                        )
                    else:
                        tail_bytes = max(0, config.monitor_new_session_tail_bytes)
                        start_offset = max(current_size - tail_bytes, 0)
                        logger.info(
                            "Started tracking session: %s (tail offset=%d size=%d)",
                            session_info.session_id,
                            start_offset,
                            current_size,
                        )
                    tracked = TrackedSession(
                        session_id=session_info.session_id,
                        file_path=str(session_info.file_path),
                        last_byte_offset=start_offset,
                    )
                    self.state.update_session(tracked)
                    self._file_mtimes[session_info.session_id] = current_mtime
                    if bootstrap:
                        continue

                # Already-tracked session on the first cycle after restart:
                # advance the read offset to EOF so the bot doesn't replay
                # whatever the agent wrote while it was down.
                if bootstrap and tracked.last_byte_offset < current_size:
                    logger.info(
                        "Bootstrap fast-forward for session %s: was offset=%d, "
                        "advancing to size=%d",
                        session_info.session_id,
                        tracked.last_byte_offset,
                        current_size,
                    )
                    tracked.last_byte_offset = current_size
                    self.state.update_session(tracked)
                    self._file_mtimes[session_info.session_id] = current_mtime
                    self._clear_partial_line_state(session_info.session_id)
                    continue

                last_mtime = self._file_mtimes.get(session_info.session_id, 0.0)
                if (
                    current_mtime <= last_mtime
                    and current_size <= tracked.last_byte_offset
                ):
                    continue

                new_entries = await self._read_new_lines(
                    tracked, session_info.file_path
                )
                self._file_mtimes[session_info.session_id] = current_mtime

                carry = self._pending_tools.get(session_info.session_id, {})
                parsed_entries, remaining = TranscriptParser.parse_entries(
                    new_entries,
                    pending_tools=carry,
                )
                if remaining:
                    self._pending_tools[session_info.session_id] = remaining
                else:
                    self._pending_tools.pop(session_info.session_id, None)

                turn_state = self._get_turn_state(session_info.session_id)

                for entry in parsed_entries:
                    stale_completion: NewMessage | None = None

                    if entry.content_type == "completion":
                        if turn_state.active_turn_id is None:
                            self._begin_turn(turn_state)
                        turn_id = turn_state.active_turn_id
                        if turn_id is None:
                            continue
                        self._queue_completion(
                            turn_state,
                            turn_id,
                            turn_had_visible_output=turn_state.active_turn_had_visible_output,
                        )
                        completion = self._emit_pending_completion(
                            session_info.session_id,
                            turn_state,
                            is_stale=False,
                        )
                        if completion:
                            new_messages.append(completion)
                        continue

                    if entry.role == "user":
                        if turn_state.active_turn_id is not None:
                            self._queue_completion(
                                turn_state,
                                turn_state.active_turn_id,
                                turn_had_visible_output=turn_state.active_turn_had_visible_output,
                            )
                        self._begin_turn(turn_state)
                        completion = self._emit_pending_completion(
                            session_info.session_id,
                            turn_state,
                            is_stale=False,
                        )
                        if completion:
                            new_messages.append(completion)
                    else:
                        if turn_state.active_turn_id is None:
                            self._begin_turn(turn_state)

                        if entry.text or entry.image_data:
                            turn_state.active_turn_had_visible_output = True
                        if (
                            turn_state.pending_completion_turn_id is not None
                            and turn_state.pending_completion_turn_id
                            != turn_state.active_turn_id
                            and turn_state.active_turn_had_visible_output
                        ):
                            stale_completion = self._emit_pending_completion(
                                session_info.session_id,
                                turn_state,
                                is_stale=True,
                            )

                    if not entry.text and not entry.image_data:
                        if stale_completion:
                            new_messages.append(stale_completion)
                        continue
                    if entry.role == "user" and not config.show_user_messages:
                        if stale_completion:
                            new_messages.append(stale_completion)
                        continue
                    new_messages.append(
                        NewMessage(
                            session_id=session_info.session_id,
                            text=entry.text,
                            is_complete=True,
                            content_type=entry.content_type,
                            tool_use_id=entry.tool_use_id,
                            role=entry.role,
                            tool_name=entry.tool_name,
                            tool_input=entry.tool_input,
                            image_data=entry.image_data,
                            timestamp=entry.timestamp,
                        )
                    )
                    if stale_completion:
                        new_messages.append(stale_completion)

                self.state.update_session(tracked)
            except OSError as e:
                logger.debug(
                    "Error processing session %s: %s", session_info.session_id, e
                )

        self.state.save_if_dirty()
        return new_messages

    async def _load_current_window_sessions(self) -> dict[str, str]:
        """Return window_id -> session_id for all monitored windows.

        Includes both Telegram-bound windows (via `thread_bindings`) and
        web-only windows tracked in `window_states`. Web sessions have no
        topic binding but still need transcript monitoring so events reach
        WebSocket subscribers.
        """
        from .session import session_manager

        window_to_session: dict[str, str] = {}
        visited: set[str] = set()
        for _user_id, _thread_id, window_id in session_manager.iter_thread_bindings():
            visited.add(window_id)
            session_id = await session_manager.refresh_window_session_if_stale(
                window_id
            )
            if session_id:
                window_to_session[window_id] = session_id
            else:
                logger.debug("No resolvable session_id for bound window %s", window_id)

        # Web-only windows (no thread binding) — still tracked here so the
        # WebSocket transport receives transcript events.
        for window_id in list(session_manager.window_states.keys()):
            if window_id in visited:
                continue
            session_id = await session_manager.refresh_window_session_if_stale(
                window_id
            )
            if session_id:
                window_to_session[window_id] = session_id

        return window_to_session

    async def _cleanup_all_stale_sessions(self) -> None:
        """Clean up tracked sessions not currently bound to any topic."""
        current_map = await self._load_current_window_sessions()
        active_session_ids = set(current_map.values())

        stale_sessions = [
            session_id
            for session_id in self.state.tracked_sessions.keys()
            if session_id not in active_session_ids
        ]
        if stale_sessions:
            logger.info(
                "[Startup cleanup] Removing %d stale sessions", len(stale_sessions)
            )
            for session_id in stale_sessions:
                self.state.remove_session(session_id)
                self._file_mtimes.pop(session_id, None)
                self._turn_state.pop(session_id, None)
                self._partial_line_state.pop(session_id, None)
            self.state.save_if_dirty()

    async def _detect_and_cleanup_changes(self) -> dict[str, str]:
        """Detect window/session changes and clean stale tracked sessions."""
        current_map = await self._load_current_window_sessions()
        sessions_to_remove: set[str] = set()

        for window_id, old_session_id in self._last_window_sessions.items():
            new_session_id = current_map.get(window_id)
            if new_session_id and new_session_id != old_session_id:
                logger.info(
                    "Window %s session changed: %s -> %s",
                    window_id,
                    old_session_id,
                    new_session_id,
                )
                sessions_to_remove.add(old_session_id)

        old_windows = set(self._last_window_sessions.keys())
        current_windows = set(current_map.keys())
        deleted_windows = old_windows - current_windows
        for window_id in deleted_windows:
            old_session_id = self._last_window_sessions[window_id]
            logger.info(
                "Window %s removed, cleaning session %s", window_id, old_session_id
            )
            sessions_to_remove.add(old_session_id)

        if sessions_to_remove:
            for session_id in sessions_to_remove:
                self.state.remove_session(session_id)
                self._file_mtimes.pop(session_id, None)
                self._turn_state.pop(session_id, None)
                self._partial_line_state.pop(session_id, None)
            self.state.save_if_dirty()

        self._last_window_sessions = current_map
        return current_map

    async def _monitor_loop(self) -> None:
        """Background polling loop."""
        global _monitor_heartbeat
        logger.info("Session monitor started, polling every %ss", self.poll_interval)

        try:
            from .session import session_manager

            await self._cleanup_all_stale_sessions()
            self._last_window_sessions = await self._load_current_window_sessions()
            bootstrap_cycle = True

            while self._running:
                _monitor_heartbeat = time.monotonic()
                try:
                    await session_manager.load_session_map()
                    current_map = await self._detect_and_cleanup_changes()
                    active_session_ids = set(current_map.values())
                    new_messages = await self.check_for_updates(
                        active_session_ids,
                        bootstrap=bootstrap_cycle,
                    )

                    for msg in new_messages:
                        status = "complete" if msg.is_complete else "streaming"
                        preview = msg.text[:80] + ("..." if len(msg.text) > 80 else "")
                        logger.info(
                            "[%s] session=%s: %s", status, msg.session_id, preview
                        )
                        # Extra listeners (web bus) are fan-out into asyncio.Queue
                        # objects — virtually instant. Dispatch them FIRST so the
                        # WebSocket subscribers don't wait behind blocking Telegram
                        # API calls in the primary callback.
                        for listener in list(self._extra_listeners):
                            try:
                                await listener(msg)
                            except Exception as e:
                                logger.error("Extra listener error: %s", e)
                        if self._message_callback:
                            try:
                                await self._message_callback(msg)
                            except Exception as e:
                                logger.error("Message callback error: %s", e)
                except Exception as e:
                    logger.error("Monitor loop error: %s", e)

                bootstrap_cycle = False
                await asyncio.sleep(self.poll_interval)

            logger.info("Session monitor stopped")
        except asyncio.CancelledError:
            logger.info("Session monitor cancelled")
            raise

    def start(self) -> None:
        if self._running:
            logger.warning("Monitor already running")
            return
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())

    async def stop(self) -> None:
        self._running = False
        task = self._task
        self._task = None
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.state.save()
        logger.info("Session monitor stopped and state saved")
