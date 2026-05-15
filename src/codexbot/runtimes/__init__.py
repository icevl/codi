"""Agent runtime registry.

Each tmux window is associated with one agent runtime — currently `codex`
or `claude`. The runtime encapsulates the bits that differ between agents:
the shell command used to start the CLI in tmux and how a session id is
discovered after launch.

Lookup is name-based (`get_runtime("claude")`) and falls back to the Codex
runtime so existing windows without a stored runtime keep working.
"""

from __future__ import annotations

from .base import AgentRuntime
from .claude import ClaudeRuntime
from .codex import CodexRuntime

_REGISTRY: dict[str, AgentRuntime] = {}


def _register(runtime: AgentRuntime) -> None:
    _REGISTRY[runtime.name] = runtime


_register(CodexRuntime())
_register(ClaudeRuntime())


DEFAULT_RUNTIME_NAME = "codex"


def get_runtime(name: str | None) -> AgentRuntime:
    """Return the runtime for ``name``, falling back to the Codex runtime."""
    if not name:
        return _REGISTRY[DEFAULT_RUNTIME_NAME]
    return _REGISTRY.get(name, _REGISTRY[DEFAULT_RUNTIME_NAME])


def all_runtimes() -> list[AgentRuntime]:
    return list(_REGISTRY.values())


__all__ = [
    "AgentRuntime",
    "CodexRuntime",
    "ClaudeRuntime",
    "DEFAULT_RUNTIME_NAME",
    "get_runtime",
    "all_runtimes",
]
