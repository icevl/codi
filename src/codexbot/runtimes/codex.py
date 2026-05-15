"""Codex agent runtime.

Wraps the existing Codex-specific behavior behind the `AgentRuntime`
interface without changing the underlying logic. Session detection in
particular is performed in `session.py` via transcript scanning; for the
Codex runtime `discover_session_id` is a no-op because the existing
`SessionManager.wait_for_session_map_entry` is still the authority.
"""

from __future__ import annotations

import logging

from ..config import config

logger = logging.getLogger(__name__)


class CodexRuntime:
    name = "codex"
    display_name = "Codex"
    display_emoji = "🔧"

    def build_start_command(self, resume_session_id: str | None) -> str:
        cmd = config.codex_command
        if resume_session_id:
            cmd = f"{cmd} resume {resume_session_id}"
        return cmd

    async def discover_session_id(
        self,
        *,
        window_id: str,
        pane_pid: int | None,
        cwd: str,
        allow_cwd_fallback: bool = True,
    ) -> str | None:
        # Codex session detection happens via SessionManager's transcript
        # scanning machinery (`wait_for_session_map_entry`), not through
        # this hook. Returning None signals the caller to use the existing
        # path, which is what we want during Phase 1.
        return None

    def pane_command_matches(self, pane_current_command: str) -> bool:
        if not isinstance(pane_current_command, str):
            return False
        return "codex" in pane_current_command.lower()
