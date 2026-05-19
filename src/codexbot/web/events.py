"""In-process pub/sub for the web transport.

Subscribers attach an asyncio queue and receive structured events. A single
publisher hooks into `SessionMonitor` and translates `NewMessage` records into
JSON-serializable payloads.

Event shapes (all dicts with `"type"` discriminator):

    {"type": "message", "window_id": "@12", "session_id": "...", "role": "...",
     "text": "...", "content_type": "text", "is_complete": true,
     "tool_name": "...", "tool_input": {...}, "tool_use_id": "...",
     "ts": 1731600000.123}

    {"type": "completion", "window_id": "@12", "session_id": "...",
     "turn_id": 3, "ts": ...}

    {"type": "sessions_changed", "ts": ...}
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from ..session_monitor import NewMessage

logger = logging.getLogger(__name__)


class EventBus:
    """Fan-out hub for web subscribers."""

    SHUTDOWN_EVENT_TYPE = "__shutdown__"

    def __init__(self, *, queue_size: int = 256) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._queue_size = queue_size
        self._closed = False

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._queue_size)
        if self._closed:
            self._put_shutdown(q)
            return q
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def _put_shutdown(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        event = {"type": self.SHUTDOWN_EVENT_TYPE}
        while True:
            try:
                q.put_nowait(event)
                return
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    return

    async def close(self) -> None:
        """Wake all subscribers so websocket handlers can exit during shutdown."""
        if self._closed:
            return
        self._closed = True
        for q in list(self._subscribers):
            self._put_shutdown(q)

    async def publish(self, event: dict[str, Any]) -> None:
        if self._closed:
            return
        event.setdefault("ts", time.time())
        dead: list[asyncio.Queue[dict[str, Any]]] = []
        for q in self._subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("Dropping event for slow subscriber (queue full)")
                dead.append(q)
        for q in dead:
            self._subscribers.discard(q)

    async def publish_sessions_changed(self) -> None:
        await self.publish({"type": "sessions_changed"})

    async def publish_message(self, msg: NewMessage, window_id: str | None) -> None:
        payload: dict[str, Any] = {
            "type": "completion" if msg.message_type == "completion" else "message",
            "window_id": window_id or "",
            "session_id": msg.session_id,
            "role": msg.role,
            "text": msg.text,
            "content_type": msg.content_type,
            "is_complete": msg.is_complete,
            "tool_name": msg.tool_name,
            "tool_input": msg.tool_input,
            "tool_use_id": msg.tool_use_id,
            "turn_id": msg.turn_id,
        }
        await self.publish(payload)


async def session_monitor_listener(bus: EventBus, msg: NewMessage) -> None:
    """Adapter passed to `SessionMonitor.add_listener`."""
    # Lazy import to avoid circular import with bot.py.
    from ..session import session_manager

    window_id: str | None = None
    for wid, ws in session_manager.window_states.items():
        if ws.session_id == msg.session_id:
            window_id = wid
            break
    await bus.publish_message(msg, window_id=window_id)
