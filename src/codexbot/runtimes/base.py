"""Base protocol for agent runtimes (Codex, Claude Code).

Phase 1 keeps the interface intentionally tiny: a runtime knows how to
produce the shell command that launches its CLI in tmux, and how to
discover the session id for a freshly created window. Later phases will
add transcript path resolution, UI patterns, and skill discovery here.
"""

from __future__ import annotations

from typing import Protocol


class AgentRuntime(Protocol):
    """Minimal agent runtime interface."""

    name: str
    display_name: str
    display_emoji: str

    def build_start_command(self, resume_session_id: str | None) -> str:
        """Return the shell command to start this agent inside a tmux pane."""
        ...

    async def discover_session_id(
        self,
        *,
        window_id: str,
        pane_pid: int | None,
        cwd: str,
        allow_cwd_fallback: bool = True,
    ) -> str | None:
        """Detect the runtime session id for a freshly created window.

        Returns the session id (UUID) or ``None`` if detection failed
        within the runtime's own timeout. Implementations may inspect
        the pane's process tree, the agent's per-process state files,
        or transcript indices.
        """
        ...

    def pane_command_matches(self, pane_current_command: str) -> bool:
        """Heuristic: does the active pane command look like this runtime?"""
        ...
