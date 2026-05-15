"""Claude Code agent runtime.

Claude Code writes a per-process file at `~/.claude/sessions/<pid>.json`
containing `{sessionId, cwd, ...}` as soon as it starts. Phase 1 uses
that file plus a process-tree walk from the tmux pane PID to discover
the runtime session id for a newly created window.

Fallback when the PID walk doesn't return a result in time: scan the
sessions directory for the most recently started entry whose `cwd`
matches the target window.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path

from ..config import config
from ..tmux_manager import tmux_manager

logger = logging.getLogger(__name__)
_SHELL_COMMANDS = {"bash", "fish", "sh", "zsh"}

_RE_BYPASS_PERMISSIONS_PROMPT = re.compile(
    r"bypass permissions mode",
    re.IGNORECASE,
)
_RE_BYPASS_ACCEPT_OPTION = re.compile(
    r"^\s*(?:❯\s*)?2\.\s+Yes,\s+I accept\b",
    re.IGNORECASE | re.MULTILINE,
)
_RE_WORKSPACE_TRUST_PROMPT = re.compile(
    r"do you trust the files in this folder\?",
    re.IGNORECASE,
)
_RE_WORKSPACE_TRUST_ACCEPT_OPTION = re.compile(
    r"^\s*(?:❯\s*)?1\.\s+Yes,\s+proceed\b",
    re.IGNORECASE | re.MULTILINE,
)


class ClaudeRuntime:
    name = "claude"
    display_name = "Claude Code"
    display_emoji = "🧠"

    def build_start_command(self, resume_session_id: str | None) -> str:
        cmd = config.claude_command
        if resume_session_id:
            cmd = f"{cmd} --resume {resume_session_id}"
        if config.claude_auto_approve_dangerous:
            if "--dangerously-skip-permissions" not in cmd:
                cmd = f"{cmd} --dangerously-skip-permissions"
        return cmd

    async def discover_session_id(
        self,
        *,
        window_id: str,
        pane_pid: int | None,
        cwd: str,
        allow_cwd_fallback: bool = True,
    ) -> str | None:
        sessions_dir = config.claude_sessions_path
        if not sessions_dir.exists():
            logger.debug("claude sessions dir does not exist: %s", sessions_dir)
            return None

        deadline = (
            asyncio.get_running_loop().time() + config.claude_session_detect_timeout
        )
        started_at_floor = time.time() - 5.0  # ignore entries written long ago
        last_startup_action_at: dict[str, float] = {}
        while asyncio.get_running_loop().time() < deadline:
            await _maybe_advance_startup_prompt(
                window_id,
                last_action_at=last_startup_action_at,
            )
            sid = await asyncio.to_thread(
                _read_claude_session_for_pane,
                pane_pid,
                cwd,
                sessions_dir,
                started_at_floor,
                allow_cwd_fallback,
            )
            if sid:
                return sid
            await asyncio.sleep(config.claude_session_detect_interval)

        logger.warning(
            "claude session detection timed out for window=%s cwd=%s pane_pid=%s",
            window_id,
            cwd,
            pane_pid,
        )
        return None

    def pane_command_matches(self, pane_current_command: str) -> bool:
        if not isinstance(pane_current_command, str):
            return False
        cmd = pane_current_command.lower()
        # `claude` may appear as `node` once the CLI is running; pane_current_command
        # is unreliable for matching, so this stays conservative.
        return cmd.startswith("claude") or cmd == "claude"


async def _maybe_advance_startup_prompt(
    window_id: str,
    *,
    last_action_at: dict[str, float],
) -> None:
    """Advance known Claude startup prompts that block session creation.

    We only auto-confirm prompts that happen before Claude starts its working
    session in tmux:
      - Bypass permissions warning (`2. Yes, I accept`)
      - Workspace trust prompt (`1. Yes, proceed`)
    """
    window = await tmux_manager.find_window_by_id(window_id)
    if not window:
        return
    if window.pane_current_command.lower() in _SHELL_COMMANDS:
        return

    pane_text = await tmux_manager.capture_pane(window_id)
    if not pane_text:
        return

    action = _classify_startup_prompt(pane_text)
    if action is None:
        return

    now = time.monotonic()
    if now - last_action_at.get(action, 0.0) < 2.0:
        return

    if action == "bypass_permissions":
        moved = await tmux_manager.send_keys(
            window_id,
            "Down",
            enter=False,
            literal=False,
        )
        if not moved:
            return
        await asyncio.sleep(0.2)
        sent = await tmux_manager.send_keys(
            window_id,
            "Enter",
            enter=False,
            literal=False,
        )
    else:
        sent = await tmux_manager.send_keys(
            window_id,
            "Enter",
            enter=False,
            literal=False,
        )
    if sent:
        last_action_at[action] = now
        logger.info(
            "auto-advanced claude startup prompt window=%s action=%s",
            window_id,
            action,
        )


def _classify_startup_prompt(pane_text: str) -> str | None:
    """Return the known startup prompt action name for a pane snapshot."""
    if _RE_BYPASS_PERMISSIONS_PROMPT.search(
        pane_text
    ) and _RE_BYPASS_ACCEPT_OPTION.search(pane_text):
        return "bypass_permissions"
    if _RE_WORKSPACE_TRUST_PROMPT.search(
        pane_text
    ) and _RE_WORKSPACE_TRUST_ACCEPT_OPTION.search(pane_text):
        return "workspace_trust"
    return None


def _read_claude_session_for_pane(
    pane_pid: int | None,
    cwd: str,
    sessions_dir: Path,
    started_at_floor: float,
    allow_cwd_fallback: bool,
) -> str | None:
    """Synchronous fast-path for session discovery.

    Walks the pane's descendant PIDs and reads
    ``~/.claude/sessions/<pid>.json``; falls back to a cwd-based scan.
    """
    candidate_pids: list[int] = []
    if pane_pid is not None:
        candidate_pids = _descendant_pids(pane_pid)
        for pid in candidate_pids:
            sid = _read_session_file_for_pid(sessions_dir, pid, cwd)
            if sid:
                return sid

    if not allow_cwd_fallback:
        return None

    # Fallback: scan all session files, filter by cwd, prefer most recent
    norm_cwd = _normalize_cwd(cwd)
    best_sid: str | None = None
    best_started_at = -1.0
    try:
        entries = list(sessions_dir.glob("*.json"))
    except OSError:
        return None

    for path in entries:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        if _normalize_cwd(data.get("cwd", "")) != norm_cwd:
            continue
        started_at_ms = data.get("startedAt")
        if not isinstance(started_at_ms, (int, float)):
            continue
        # startedAt is ms since epoch; filter recent entries
        started_at = started_at_ms / 1000.0
        if started_at < started_at_floor:
            continue
        if started_at > best_started_at:
            sid = data.get("sessionId")
            if isinstance(sid, str) and sid:
                best_sid = sid
                best_started_at = started_at
    return best_sid


def _read_session_file_for_pid(sessions_dir: Path, pid: int, cwd: str) -> str | None:
    path = sessions_dir / f"{pid}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if _normalize_cwd(data.get("cwd", "")) != _normalize_cwd(cwd):
        # The pane's claude process may have started in a different cwd
        # than the window's recorded cwd. Trust the PID match.
        logger.debug(
            "claude session file pid=%s cwd mismatch: file=%s window=%s",
            pid,
            data.get("cwd"),
            cwd,
        )
    sid = data.get("sessionId")
    if isinstance(sid, str) and sid:
        return sid
    return None


def _descendant_pids(root_pid: int) -> list[int]:
    """Return ``root_pid``'s descendants ordered breadth-first.

    Shells out to ``ps`` once and walks the parent/child relations in
    Python. The list excludes ``root_pid`` itself.
    """
    import subprocess

    try:
        out = subprocess.check_output(
            ["ps", "-A", "-o", "pid=,ppid="],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2.0,
        )
    except (subprocess.SubprocessError, OSError):
        return []

    children: dict[int, list[int]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        children.setdefault(ppid, []).append(pid)

    result: list[int] = []
    queue: list[int] = list(children.get(root_pid, []))
    while queue:
        pid = queue.pop(0)
        result.append(pid)
        queue.extend(children.get(pid, []))
    return result


def _normalize_cwd(cwd: str) -> str:
    try:
        return str(Path(cwd).expanduser().resolve())
    except (OSError, RuntimeError, ValueError):
        return cwd
