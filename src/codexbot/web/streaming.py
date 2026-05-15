"""Live pane streaming for web subscribers.

JSONL transcripts only get flushed when a content block finishes, so we can't
stream tokens from them. Instead, while a runtime is generating we sample its
tmux pane every few hundred ms, strip the bottom chrome, and publish the
visible body to the event bus. The web UI renders it as a "streaming" bubble
that is replaced when the real message lands via JSONL.

Active = `parse_status_line()` returns a status (the "esc to interrupt"
footer). When the status disappears, we publish a `stream_end` event so the
frontend can drop its placeholder.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

from ..session import session_manager
from ..terminal_parser import STATUS_PREFIX_CHARS, parse_status_line, strip_pane_chrome
from ..tmux_manager import tmux_manager

if TYPE_CHECKING:
    from .events import EventBus

logger = logging.getLogger(__name__)

STREAM_POLL_INTERVAL = 0.3
STREAM_BODY_MAX_LINES = 120

_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
# Codex echoes the most recent user input as a `> ...` block; Claude uses
# `> ` similarly. We treat the LAST such block as a separator: everything
# below it is the in-progress assistant output.
_USER_ECHO_RE = re.compile(r"^\s*>\s")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _is_status_spinner_line(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and stripped[0] in STATUS_PREFIX_CHARS


def _extract_stream_body(pane_text: str) -> str:
    """Pick the visible assistant region from a captured pane.

    The status line (spinner + "esc to interrupt") is already published in the
    `status` field of the stream event, so we drop it from the body to avoid
    rendering it twice.
    """
    lines = _strip_ansi(pane_text).split("\n")
    lines = strip_pane_chrome(lines)
    while lines and not lines[-1].strip():
        lines.pop()

    last_user_idx: int | None = None
    for i in range(len(lines) - 1, -1, -1):
        if _USER_ECHO_RE.match(lines[i]):
            last_user_idx = i
            break
    if last_user_idx is not None:
        cursor = last_user_idx + 1
        while cursor < len(lines) and (
            not lines[cursor].strip() or _USER_ECHO_RE.match(lines[cursor])
        ):
            cursor += 1
        lines = lines[cursor:]

    while lines and _is_status_spinner_line(lines[-1]):
        lines.pop()
    while lines and not lines[-1].strip():
        lines.pop()

    if len(lines) > STREAM_BODY_MAX_LINES:
        lines = lines[-STREAM_BODY_MAX_LINES:]
    return "\n".join(lines).rstrip()


async def stream_pane_loop(bus: EventBus, *, poll_interval: float | None = None) -> None:
    """Poll tmux panes during active turns and publish stream events.

    Emits:
      {"type": "stream", window_id, session_id, text, status}
        on body change while a status line is present.
      {"type": "stream_end", window_id, session_id}
        once when the status line disappears for a window we were streaming.
    """
    interval = poll_interval if poll_interval is not None else STREAM_POLL_INTERVAL
    last_body: dict[str, str] = {}
    active: set[str] = set()

    logger.info("Pane streaming started (interval: %ss)", interval)
    while True:
        try:
            for window_id, ws in list(session_manager.window_states.items()):
                try:
                    w = await tmux_manager.find_window_by_id(window_id)
                    if not w:
                        if window_id in active:
                            active.discard(window_id)
                            last_body.pop(window_id, None)
                            await bus.publish(
                                {
                                    "type": "stream_end",
                                    "window_id": window_id,
                                    "session_id": ws.session_id,
                                }
                            )
                        continue

                    pane = await tmux_manager.capture_pane(w.window_id)
                    if not pane:
                        continue

                    status = parse_status_line(pane)
                    if not status:
                        if window_id in active:
                            active.discard(window_id)
                            last_body.pop(window_id, None)
                            await bus.publish(
                                {
                                    "type": "stream_end",
                                    "window_id": window_id,
                                    "session_id": ws.session_id,
                                }
                            )
                        continue

                    body = _extract_stream_body(pane)
                    if body == last_body.get(window_id):
                        active.add(window_id)
                        continue

                    last_body[window_id] = body
                    active.add(window_id)
                    await bus.publish(
                        {
                            "type": "stream",
                            "window_id": window_id,
                            "session_id": ws.session_id,
                            "text": body,
                            "status": status,
                        }
                    )
                except Exception as e:
                    logger.debug(
                        "pane stream loop error window=%s: %s", window_id, e
                    )
        except asyncio.CancelledError:
            logger.info("Pane streaming cancelled")
            raise
        except Exception as e:
            logger.error("Pane stream loop error: %s", e)
        await asyncio.sleep(interval)
