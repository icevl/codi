"""Per-user message queue management for ordered message delivery.

Provides a queue-based message processing system that ensures:
  - Messages are sent in receive order (FIFO)
  - Status messages always follow content messages
  - Consecutive content messages can be merged for efficiency
  - Thread-aware sending: each MessageTask carries an optional thread_id
    for Telegram topic support

Rate limiting is handled globally by AIORateLimiter on the Application.

Key components:
  - MessageTask: Dataclass representing a queued message task (with thread_id)
  - get_or_create_queue: Get or create queue and worker for a user
  - Message queue worker: Background task processing user's queue
  - Content task processing with tool_use/tool_result handling
  - Status message tracking and conversion (keyed by (user_id, thread_id))
"""

import asyncio
from collections import deque
import logging
import time
from dataclasses import dataclass, field
from typing import Literal

from telegram import Bot
from telegram.constants import ChatAction
from telegram.error import RetryAfter

from ..config import config
from ..markdown_v2 import convert_markdown
from ..session import session_manager
from ..terminal_parser import parse_status_line
from ..tmux_manager import tmux_manager
from .message_sender import (
    NO_LINK_PREVIEW,
    PARSE_MODE,
    send_photo,
    send_with_fallback,
    strip_sentinels,
)

logger = logging.getLogger(__name__)


def _ensure_formatted(text: str) -> str:
    """Convert markdown to MarkdownV2."""
    return convert_markdown(text)


# Merge limit for content messages
MERGE_MAX_LENGTH = 3800  # Leave room for markdown conversion overhead
# Telegram completion deliveries should fail fast unless explicit success is observed.
# _process_content_task returns None when send_with_fallback cannot send; this class
# lets the completion retry loop treat that as a retryable transport-style failure.


class CompletionDeliveryError(RuntimeError):
    """Raised when a queued completion message could not be sent."""


# Max completions to remember per lane/session before compacting history.
COMPLETION_TURN_WINDOW = 256
# Number of diagnostic events retained per (user_id, thread_id) lane.
COMPLETION_DIAGNOSTIC_EVENT_MAXLEN = 64
# Retry cap for transient completion send errors.
COMPLETION_RETRY_ATTEMPTS = 3
# Base retry delay (in seconds) for retryable completion delivery failures.
COMPLETION_RETRY_DELAY_SECONDS = 0.25


@dataclass
class MessageTask:
    """Message task for queue processing."""

    task_type: Literal["content", "completion", "status_update", "status_clear"]
    text: str | None = None
    window_id: str | None = None
    # content type fields
    parts: list[str] = field(default_factory=list)
    tool_use_id: str | None = None
    content_type: str = "text"
    session_id: str | None = None
    turn_id: int | None = None
    thread_id: int | None = None  # Telegram topic thread_id for targeted send
    image_data: list[tuple[str, bytes]] | None = None  # From tool_result images
    convert_status_to_content: bool = True


# Per-(user, thread) message queues and worker tasks
_message_queues: dict[tuple[int, int], asyncio.Queue[MessageTask]] = {}
_queue_workers: dict[tuple[int, int], asyncio.Task[None]] = {}
_queue_locks: dict[
    tuple[int, int], asyncio.Lock
] = {}  # Protect drain/refill operations

# Map (tool_use_id, user_id, thread_id_or_0) -> telegram message_id
# for editing tool_use messages with results
_tool_msg_ids: dict[tuple[str, int, int], int] = {}

# Status message tracking: (user_id, thread_id_or_0) -> (message_id, window_id, last_text)
_status_msg_info: dict[tuple[int, int], tuple[int, str, str]] = {}

# Flood control: (user_id, thread_id_or_0) -> monotonic time when ban expires
_flood_until: dict[tuple[int, int], float] = {}
# Dropped status updates during queue pressure per lane.
_status_drop_counts: dict[tuple[int, int], int] = {}

# Completion lifecycle tracking: queue lane + session -> turn ids.
_completion_pending_turns: dict[tuple[int, int], dict[str, set[int]]] = {}
_completion_complete_turns: dict[tuple[int, int], dict[str, set[int]]] = {}
_completion_turn_lock = asyncio.Lock()
# Completion lifecycle diagnostics: bounded per-lane in-memory event log.
_completion_diagnostic_events: dict[
    tuple[int, int], deque[dict[str, str | int | float | bool | None]]
] = {}

# Max seconds to wait for flood control before dropping tasks
FLOOD_CONTROL_MAX_WAIT = 10


def _queue_key(user_id: int, thread_id: int | None = None) -> tuple[int, int]:
    """Build a stable queue key for a user/thread lane."""
    return (user_id, thread_id or 0)


def get_message_queue(
    user_id: int, thread_id: int | None = None
) -> asyncio.Queue[MessageTask] | None:
    """Get the message queue for a user/thread lane (if exists)."""
    return _message_queues.get(_queue_key(user_id, thread_id))


def get_or_create_queue(
    bot: Bot, user_id: int, thread_id: int | None = None
) -> asyncio.Queue[MessageTask]:
    """Get or create message queue and worker for a user/thread lane."""
    key = _queue_key(user_id, thread_id)
    if key not in _message_queues:
        _message_queues[key] = asyncio.Queue(maxsize=max(1, config.queue_maxsize))
        _queue_locks[key] = asyncio.Lock()
        # Start worker task for this user/thread lane
        _queue_workers[key] = asyncio.create_task(
            _message_queue_worker(bot, key[0], key[1])
        )
    return _message_queues[key]


def _inspect_queue(queue: asyncio.Queue[MessageTask]) -> list[MessageTask]:
    """Non-destructively inspect all items in queue.

    Drains the queue and returns all items. Caller must refill.
    """
    items: list[MessageTask] = []
    while not queue.empty():
        try:
            item = queue.get_nowait()
            items.append(item)
        except asyncio.QueueEmpty:
            break
    return items


def _drop_pending_status_tasks(
    queue: asyncio.Queue[MessageTask], key: tuple[int, int]
) -> int:
    """Drop queued status tasks and keep non-status tasks order intact."""
    items = _inspect_queue(queue)
    if not items:
        return 0

    kept: list[MessageTask] = []
    dropped = 0
    for item in items:
        if item.task_type in ("status_update", "status_clear"):
            dropped += 1
            continue
        kept.append(item)

    for item in kept:
        queue.put_nowait(item)
        # Compensate unfinished_tasks increment from re-put item
        queue.task_done()

    for _ in range(dropped):
        # Dropped items were previously enqueued and must be marked done
        queue.task_done()

    if dropped:
        _status_drop_counts[key] = _status_drop_counts.get(key, 0) + dropped
    return dropped


async def _enqueue_with_pressure_policy(
    queue: asyncio.Queue[MessageTask],
    lock: asyncio.Lock,
    task: MessageTask,
    user_id: int,
    thread_id: int | None,
) -> bool:
    """Enqueue tasks with bounded-pressure policy.

    - content/completion are never dropped (await queue.put on pressure)
    - status tasks are coalesced/dropped under pressure
    """
    key = _queue_key(user_id, thread_id)
    try:
        queue.put_nowait(task)
        return True
    except asyncio.QueueFull:
        pass

    if task.task_type in ("status_update", "status_clear"):
        async with lock:
            dropped_existing = _drop_pending_status_tasks(queue, key)
            try:
                queue.put_nowait(task)
                if dropped_existing:
                    logger.debug(
                        "Dropped %d queued status task(s) for user=%d thread=%d",
                        dropped_existing,
                        user_id,
                        key[1],
                    )
                return True
            except asyncio.QueueFull:
                _status_drop_counts[key] = _status_drop_counts.get(key, 0) + 1
                logger.debug(
                    "Dropped incoming status task due saturated queue user=%d thread=%d",
                    user_id,
                    key[1],
                )
                return False

    await queue.put(task)
    return True


def _normalize_retry_after(error: RetryAfter) -> int:
    """Return Flood error delay seconds as int."""
    if isinstance(error.retry_after, int):
        return error.retry_after
    return int(error.retry_after.total_seconds())


def _is_retryable_completion_error(exc: Exception) -> bool:
    """Return True for recoverable transient errors while sending completion tasks."""
    return isinstance(
        exc,
        (RetryAfter, TimeoutError, OSError, ConnectionError, CompletionDeliveryError),
    )


def _completion_state(
    pending: bool, user_id: int, thread_id_or_0: int, session_id: str
) -> set[int]:
    """Return mutable set for the requested completion lifecycle bucket."""
    store = (
        _completion_pending_turns if pending else _completion_complete_turns
    ).setdefault((user_id, thread_id_or_0), {})
    return store.setdefault(session_id, set())


def _trim_completion_turns(turns: set[int]) -> set[int]:
    """Keep most recent turns to avoid unbounded per-session memory growth."""
    if len(turns) <= COMPLETION_TURN_WINDOW * 2:
        return turns
    newest = max(turns)
    threshold = newest - COMPLETION_TURN_WINDOW
    return {turn for turn in turns if turn >= threshold}


def _record_completion_diagnostic_event(
    *,
    user_id: int,
    thread_id_or_0: int,
    event: str,
    task_type: str,
    window_id: str | None,
    session_id: str | None,
    turn_id: int | None,
    queue_attempt: int,
    reason: str | None = None,
) -> None:
    """Record one completion completion lifecycle diagnostic event.

    The buffer is bounded per lane so events stay bounded in memory.
    """
    bucket = _completion_diagnostic_events.setdefault(
        (user_id, thread_id_or_0),
        deque(maxlen=COMPLETION_DIAGNOSTIC_EVENT_MAXLEN),
    )
    bucket.append(
        {
            "timestamp": time.time(),
            "event": event,
            "task_type": task_type,
            "window_id": window_id,
            "session_id": session_id,
            "turn_id": turn_id,
            "queue_attempt": queue_attempt,
            "reason": reason,
        }
    )


def get_diagnostic_events(
    user_id: int, thread_id: int | None = None
) -> list[dict[str, str | int | float | bool | None]]:
    """Return a copy of recent diagnostic events for topic lane."""
    return list(_completion_diagnostic_events.get(_queue_key(user_id, thread_id), []))


def get_queue_health(
    user_id: int, thread_id: int | None = None
) -> dict[str, int | float | bool]:
    """Return queue health snapshot for diagnostics."""
    key = _queue_key(user_id, thread_id)
    queue = _message_queues.get(key)
    worker = _queue_workers.get(key)
    flood_end = _flood_until.get(key, 0.0)
    flood_remaining = max(0.0, flood_end - time.monotonic())
    return {
        "depth": queue.qsize() if queue else 0,
        "maxsize": queue.maxsize if queue else max(1, config.queue_maxsize),
        "worker_alive": bool(worker and not worker.done()),
        "flood_remaining_seconds": flood_remaining,
        "dropped_status": _status_drop_counts.get(key, 0),
    }


def format_diagnostic_events(
    events: list[dict[str, str | int | float | bool | None]],
    *,
    max_events: int = 10,
) -> list[str]:
    """Render diagnostic events into compact operator-visible lines."""
    if not events:
        return []
    output: list[str] = []
    for event in events[-max_events:]:
        raw_ts = event.get("timestamp", 0)
        ts_value = float(raw_ts) if isinstance(raw_ts, (int, float)) else 0.0
        ts = time.strftime("%H:%M:%S", time.localtime(ts_value))
        turn = event.get("turn_id")
        reason = event.get("reason")
        output.append(
            f"{ts} t={turn if turn is not None else '-'} "
            f"{event.get('task_type', '-')}:{event.get('event', '-')}:"
            f"{reason if reason is not None else '-'} "
            f"a={event.get('queue_attempt', 0)}"
        )
    return output


async def _claim_completion_turn(
    user_id: int, thread_id_or_0: int, session_id: str, turn_id: int
) -> bool:
    """Claim completion marker for a session/turn once per queue lane."""
    async with _completion_turn_lock:
        seen = _completion_state(False, user_id, thread_id_or_0, session_id)
        if turn_id in seen:
            return False

        pending = _completion_state(True, user_id, thread_id_or_0, session_id)
        if turn_id in pending:
            return False
        pending.add(turn_id)
        return True


async def _complete_completion_turn(
    user_id: int, thread_id_or_0: int, session_id: str, turn_id: int
) -> None:
    """Mark a claimed completion as successfully delivered."""
    async with _completion_turn_lock:
        lane = (user_id, thread_id_or_0)
        pending_session = _completion_pending_turns.get(lane, {})
        if session_pending := pending_session.get(session_id):
            session_pending.discard(turn_id)
            if not session_pending:
                pending_session.pop(session_id, None)
                if not pending_session:
                    _completion_pending_turns.pop(lane, None)

        seen_session = _completion_complete_turns.setdefault(lane, {}).setdefault(
            session_id, set()
        )
        seen_session.add(turn_id)
        _completion_complete_turns[lane][session_id] = _trim_completion_turns(
            seen_session
        )


async def _release_completion_turn(
    user_id: int, thread_id_or_0: int, session_id: str, turn_id: int
) -> None:
    """Release a claimed completion turn when sending fails."""
    async with _completion_turn_lock:
        pending = _completion_pending_turns.get((user_id, thread_id_or_0))
        if not pending:
            return
        turns = pending.get(session_id)
        if not turns:
            return
        turns.discard(turn_id)
        if not turns:
            pending.pop(session_id, None)
            if not pending:
                _completion_pending_turns.pop((user_id, thread_id_or_0), None)


def _can_merge_tasks(base: MessageTask, candidate: MessageTask) -> bool:
    """Check if two content tasks can be merged."""
    if base.window_id != candidate.window_id:
        return False
    if candidate.task_type != "content":
        return False
    # tool_use/tool_result break merge chain
    # - tool_use: will be edited later by tool_result
    # - tool_result: edits previous message, merging would cause order issues
    if base.content_type in ("tool_use", "tool_result", "completion"):
        return False
    if candidate.content_type in ("tool_use", "tool_result", "completion"):
        return False
    if base.convert_status_to_content != candidate.convert_status_to_content:
        return False
    return True


async def _merge_content_tasks(
    queue: asyncio.Queue[MessageTask],
    first: MessageTask,
    lock: asyncio.Lock,
) -> tuple[MessageTask, int]:
    """Merge consecutive content tasks from queue.

    Returns: (merged_task, merge_count) where merge_count is the number of
    additional tasks merged (0 if no merging occurred).

    Note on queue counter management:
        When we put items back, we call task_done() to compensate for the
        internal counter increment caused by put_nowait(). This is necessary
        because the items were already counted when originally enqueued.
        Without this compensation, queue.join() would wait indefinitely.
    """
    merged_parts = list(first.parts)
    current_length = sum(len(p) for p in merged_parts)
    merge_count = 0

    async with lock:
        items = _inspect_queue(queue)
        remaining: list[MessageTask] = []

        for i, task in enumerate(items):
            if not _can_merge_tasks(first, task):
                # Can't merge, keep this and all remaining items
                remaining = items[i:]
                break

            # Check length before merging
            task_length = sum(len(p) for p in task.parts)
            if current_length + task_length > MERGE_MAX_LENGTH:
                # Too long, stop merging
                remaining = items[i:]
                break

            merged_parts.extend(task.parts)
            current_length += task_length
            merge_count += 1

        # Put remaining items back into the queue
        for item in remaining:
            queue.put_nowait(item)
            # Compensate: this item was already counted when first enqueued,
            # put_nowait adds a duplicate count that must be removed
            queue.task_done()

    if merge_count == 0:
        return first, 0

    return (
        MessageTask(
            task_type="content",
            window_id=first.window_id,
            parts=merged_parts,
            tool_use_id=first.tool_use_id,
            content_type=first.content_type,
            thread_id=first.thread_id,
            convert_status_to_content=first.convert_status_to_content,
        ),
        merge_count,
    )


async def _message_queue_worker(bot: Bot, user_id: int, thread_id_or_0: int) -> None:
    """Process message tasks for a user/thread lane sequentially."""
    queue = _message_queues[(user_id, thread_id_or_0)]
    lock = _queue_locks[(user_id, thread_id_or_0)]
    logger.info(
        "Message queue worker started for user %d thread %d",
        user_id,
        thread_id_or_0,
    )

    consecutive_errors = 0

    while True:
        try:
            task = await queue.get()
            consecutive_errors = 0
            queue_key = (user_id, thread_id_or_0)
            attempt = 0
            completion_claimed = False
            completion_processed = False
            try:
                while True:
                    try:
                        # Flood control: drop status, wait for content
                        flood_end = _flood_until.get(queue_key, 0)
                        if flood_end > 0:
                            remaining = flood_end - time.monotonic()
                            if remaining > 0:
                                if task.task_type not in ("content", "completion"):
                                    # Status is ephemeral — safe to drop
                                    break
                                logger.debug(
                                    "Flood controlled: waiting %.0fs for content "
                                    "(user=%d thread=%d)",
                                    remaining,
                                    user_id,
                                    thread_id_or_0,
                                )
                                await asyncio.sleep(remaining)
                            _flood_until.pop(queue_key, None)
                            logger.info(
                                "Flood control lifted for user %d thread %d",
                                user_id,
                                thread_id_or_0,
                            )

                        if task.task_type == "content":
                            merged_task, merge_count = await _merge_content_tasks(
                                queue, task, lock
                            )
                            if merge_count > 0:
                                logger.debug(
                                    "Merged %d tasks for user=%d thread=%d",
                                    merge_count,
                                    user_id,
                                    thread_id_or_0,
                                )
                                for _ in range(merge_count):
                                    queue.task_done()
                            await _process_content_task(bot, user_id, merged_task)
                            _record_completion_diagnostic_event(
                                user_id=user_id,
                                thread_id_or_0=thread_id_or_0,
                                event="sent",
                                task_type="content",
                                window_id=merged_task.window_id,
                                session_id=merged_task.session_id,
                                turn_id=merged_task.turn_id,
                                queue_attempt=attempt,
                                reason="content_processed",
                            )
                            break

                        if task.task_type == "completion":
                            if (
                                task.session_id is not None
                                and task.turn_id is not None
                                and not completion_claimed
                            ):
                                claimed = await _claim_completion_turn(
                                    user_id,
                                    thread_id_or_0,
                                    task.session_id,
                                    task.turn_id,
                                )
                                if not claimed:
                                    logger.debug(
                                        "Skipping duplicate completion for user=%s session=%s turn=%s thread=%s",
                                        user_id,
                                        task.session_id,
                                        task.turn_id,
                                        thread_id_or_0,
                                    )
                                    _record_completion_diagnostic_event(
                                        user_id=user_id,
                                        thread_id_or_0=thread_id_or_0,
                                        event="duplicate_skipped",
                                        task_type="completion",
                                        window_id=task.window_id,
                                        session_id=task.session_id,
                                        turn_id=task.turn_id,
                                        queue_attempt=attempt,
                                        reason="already_claimed",
                                    )
                                    break
                                completion_claimed = True
                                _record_completion_diagnostic_event(
                                    user_id=user_id,
                                    thread_id_or_0=thread_id_or_0,
                                    event="claimed",
                                    task_type="completion",
                                    window_id=task.window_id,
                                    session_id=task.session_id,
                                    turn_id=task.turn_id,
                                    queue_attempt=attempt,
                                )

                            await _process_content_task(bot, user_id, task)
                            _record_completion_diagnostic_event(
                                user_id=user_id,
                                thread_id_or_0=thread_id_or_0,
                                event="sent",
                                task_type="completion",
                                window_id=task.window_id,
                                session_id=task.session_id,
                                turn_id=task.turn_id,
                                queue_attempt=attempt,
                                reason="completion_processed",
                            )
                            completion_processed = True
                            break

                        if task.task_type == "status_update":
                            await _process_status_update_task(bot, user_id, task)
                            break

                        if task.task_type == "status_clear":
                            await _do_clear_status_message(
                                bot, user_id, task.thread_id or 0
                            )
                            break

                        logger.debug(
                            "Skipping unknown queue task type=%s user=%d thread=%d",
                            task.task_type,
                            user_id,
                            thread_id_or_0,
                        )
                        break

                    except RetryAfter as e:
                        if (
                            task.task_type != "completion"
                            or attempt + 1 >= COMPLETION_RETRY_ATTEMPTS
                        ):
                            raise

                        attempt += 1
                        delay = _normalize_retry_after(e)
                        if delay > FLOOD_CONTROL_MAX_WAIT:
                            _flood_until[queue_key] = time.monotonic() + delay
                            logger.warning(
                                "Flood control for user %d thread %d: retry_after=%ds, "
                                "pausing queue until ban expires",
                                user_id,
                                thread_id_or_0,
                                delay,
                            )
                        else:
                            logger.warning(
                                "Retrying completion for user %d thread %d in %ds",
                                user_id,
                                thread_id_or_0,
                                delay,
                            )
                            _record_completion_diagnostic_event(
                                user_id=user_id,
                                thread_id_or_0=thread_id_or_0,
                                event="retrying",
                                task_type="completion",
                                window_id=task.window_id,
                                session_id=task.session_id,
                                turn_id=task.turn_id,
                                queue_attempt=attempt,
                                reason=f"RetryAfter:{delay}",
                            )

                        await asyncio.sleep(delay)
                        continue

                    except Exception as e:
                        if (
                            task.task_type != "completion"
                            or not _is_retryable_completion_error(e)
                            or attempt + 1 >= COMPLETION_RETRY_ATTEMPTS
                        ):
                            raise

                        attempt += 1
                        delay = COMPLETION_RETRY_DELAY_SECONDS * attempt
                        logger.warning(
                            "Retrying completion for user %d thread %d in %.2fs after %s",
                            user_id,
                            thread_id_or_0,
                            delay,
                            type(e).__name__,
                        )
                        _record_completion_diagnostic_event(
                            user_id=user_id,
                            thread_id_or_0=thread_id_or_0,
                            event="retrying",
                            task_type="completion",
                            window_id=task.window_id,
                            session_id=task.session_id,
                            turn_id=task.turn_id,
                            queue_attempt=attempt,
                            reason=type(e).__name__,
                        )
                        await asyncio.sleep(delay)
                        continue

                if (
                    completion_processed
                    and task.session_id is not None
                    and task.turn_id is not None
                ):
                    await _complete_completion_turn(
                        user_id,
                        thread_id_or_0,
                        task.session_id,
                        task.turn_id,
                    )
                    _record_completion_diagnostic_event(
                        user_id=user_id,
                        thread_id_or_0=thread_id_or_0,
                        event="finalized",
                        task_type="completion",
                        window_id=task.window_id,
                        session_id=task.session_id,
                        turn_id=task.turn_id,
                        queue_attempt=attempt,
                        reason="completion_complete",
                    )
            except Exception as e:
                if (
                    task.task_type == "completion"
                    and completion_claimed
                    and task.session_id is not None
                    and task.turn_id is not None
                ):
                    await _release_completion_turn(
                        user_id,
                        thread_id_or_0,
                        task.session_id,
                        task.turn_id,
                    )
                    _record_completion_diagnostic_event(
                        user_id=user_id,
                        thread_id_or_0=thread_id_or_0,
                        event="release",
                        task_type="completion",
                        window_id=task.window_id,
                        session_id=task.session_id,
                        turn_id=task.turn_id,
                        queue_attempt=attempt,
                        reason=str(e),
                    )
                if task.task_type == "completion" and task.session_id is not None:
                    _record_completion_diagnostic_event(
                        user_id=user_id,
                        thread_id_or_0=thread_id_or_0,
                        event="error",
                        task_type="completion",
                        window_id=task.window_id,
                        session_id=task.session_id,
                        turn_id=task.turn_id,
                        queue_attempt=attempt,
                        reason=str(e),
                    )
                logger.error(
                    "Error processing message task for user %d thread %d: %s",
                    user_id,
                    thread_id_or_0,
                    e,
                )
            finally:
                queue.task_done()
        except asyncio.CancelledError:
            logger.info(
                "Message queue worker cancelled for user %d thread %d",
                user_id,
                thread_id_or_0,
            )
            break
        except Exception as e:
            consecutive_errors += 1
            # If queue.get() itself keeps failing (event loop mismatch, etc),
            # the loop would spin synchronously and flood logs. Back off, and
            # after a few failures tear the lane down so the next enqueue can
            # rebuild it on a healthy loop.
            if consecutive_errors >= 5:
                logger.error(
                    "Queue worker for user %d thread %d failed %d times in a row "
                    "(%s); tearing down lane",
                    user_id,
                    thread_id_or_0,
                    consecutive_errors,
                    e,
                )
                key = (user_id, thread_id_or_0)
                _message_queues.pop(key, None)
                _queue_locks.pop(key, None)
                _queue_workers.pop(key, None)
                break
            logger.error(
                "Unexpected error in queue worker for user %d thread %d (attempt %d): %s",
                user_id,
                thread_id_or_0,
                consecutive_errors,
                e,
            )
            await asyncio.sleep(min(2**consecutive_errors, 30))


def _send_kwargs(thread_id: int | None) -> dict[str, int]:
    """Build message_thread_id kwargs for bot.send_message()."""
    if thread_id is not None:
        return {"message_thread_id": thread_id}
    return {}


async def _send_task_images(bot: Bot, chat_id: int, task: MessageTask) -> None:
    """Send images attached to a task, if any."""
    if not task.image_data:
        return
    logger.info(
        "Sending %d image(s) in thread %s",
        len(task.image_data),
        task.thread_id,
    )
    await send_photo(
        bot,
        chat_id,
        task.image_data,
        **_send_kwargs(task.thread_id),  # type: ignore[arg-type]
    )


async def _process_content_task(bot: Bot, user_id: int, task: MessageTask) -> None:
    """Process a content message task."""
    wid = task.window_id or ""
    tid = task.thread_id or 0
    chat_id = session_manager.resolve_chat_id(user_id, task.thread_id)
    completion_sent = False

    # 1. Handle tool_result editing (merged parts are edited together)
    if task.content_type == "tool_result" and task.tool_use_id:
        _tkey = (task.tool_use_id, user_id, tid)
        edit_msg_id = _tool_msg_ids.pop(_tkey, None)
        if edit_msg_id is not None:
            # Clear status message first
            await _do_clear_status_message(bot, user_id, tid)
            # Join all parts for editing (merged content goes together)
            full_text = "\n\n".join(task.parts)
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=edit_msg_id,
                    text=_ensure_formatted(full_text),
                    parse_mode=PARSE_MODE,
                    link_preview_options=NO_LINK_PREVIEW,
                )
                await _send_task_images(bot, chat_id, task)
                await _check_and_send_status(bot, user_id, wid, task.thread_id)
                return
            except RetryAfter:
                raise
            except Exception:
                try:
                    # Fallback: plain text with sentinels stripped
                    plain_text = strip_sentinels(task.text or full_text)
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=edit_msg_id,
                        text=plain_text,
                        link_preview_options=NO_LINK_PREVIEW,
                    )
                    await _send_task_images(bot, chat_id, task)
                    await _check_and_send_status(bot, user_id, wid, task.thread_id)
                    return
                except RetryAfter:
                    raise
                except Exception:
                    logger.debug(f"Failed to edit tool msg {edit_msg_id}, sending new")
                    # Fall through to send as new message

    # 2. Send content messages, converting status message to first content part
    first_part = True
    last_msg_id: int | None = None
    for part in task.parts:
        sent = None

        # For first part, try to convert status message to content (edit instead of delete)
        if first_part:
            first_part = False
            if task.convert_status_to_content:
                converted_msg_id = await _convert_status_to_content(
                    bot,
                    user_id,
                    tid,
                    wid,
                    part,
                )
                if converted_msg_id is not None:
                    last_msg_id = converted_msg_id
                    if task.task_type == "completion":
                        completion_sent = True
                    continue

        sent = await send_with_fallback(
            bot,
            chat_id,
            part,
            **_send_kwargs(task.thread_id),  # type: ignore[arg-type]
        )
        if task.task_type == "completion" and sent is None:
            raise CompletionDeliveryError(
                f"Failed to send completion message (user={user_id} thread={tid})"
            )

        if sent:
            last_msg_id = sent.message_id
            if task.task_type == "completion":
                completion_sent = True

    # 3. Record tool_use message ID for later editing
    if last_msg_id and task.tool_use_id and task.content_type == "tool_use":
        _tool_msg_ids[(task.tool_use_id, user_id, tid)] = last_msg_id

    # 4. Send images if present (from tool_result with base64 image blocks)
    try:
        await _send_task_images(bot, chat_id, task)
        # 5. After content, check and send status
        await _check_and_send_status(bot, user_id, wid, task.thread_id)
    except Exception as exc:
        # If completion text is already delivered, do not resend it on
        # status-refresh errors from downstream calls.
        if task.task_type == "completion" and completion_sent:
            logger.warning(
                "Suppressing post-send completion error for user %d thread %d: %s",
                user_id,
                tid,
                type(exc).__name__,
            )
            return
        raise


async def _convert_status_to_content(
    bot: Bot,
    user_id: int,
    thread_id_or_0: int,
    window_id: str,
    content_text: str,
) -> int | None:
    """Convert status message to content message by editing it.

    Returns the message_id if converted successfully, None otherwise.
    """
    skey = (user_id, thread_id_or_0)
    info = _status_msg_info.pop(skey, None)
    if not info:
        return None

    msg_id, stored_wid, _ = info
    chat_id = session_manager.resolve_chat_id(user_id, thread_id_or_0 or None)
    if stored_wid != window_id:
        # Different window, just delete the old status
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass
        return None

    # Edit status message to show content
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=_ensure_formatted(content_text),
            parse_mode=PARSE_MODE,
            link_preview_options=NO_LINK_PREVIEW,
        )
        return msg_id
    except RetryAfter:
        raise
    except Exception:
        try:
            # Fallback to plain text with sentinels stripped
            plain = strip_sentinels(content_text)
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=plain,
                link_preview_options=NO_LINK_PREVIEW,
            )
            return msg_id
        except RetryAfter:
            raise
        except Exception as e:
            logger.debug(f"Failed to convert status to content: {e}")
            # Message might be deleted or too old, caller will send new message
            return None


async def _process_status_update_task(
    bot: Bot, user_id: int, task: MessageTask
) -> None:
    """Process a status update task."""
    wid = task.window_id or ""
    tid = task.thread_id or 0
    chat_id = session_manager.resolve_chat_id(user_id, task.thread_id)
    skey = (user_id, tid)
    status_text = task.text or ""

    if not status_text:
        # No status text means clear status
        await _do_clear_status_message(bot, user_id, tid)
        return

    current_info = _status_msg_info.get(skey)

    if current_info:
        msg_id, stored_wid, last_text = current_info

        if stored_wid != wid:
            # Window changed - delete old and send new
            await _do_clear_status_message(bot, user_id, tid)
            await _do_send_status_message(bot, user_id, tid, wid, status_text)
        elif status_text == last_text:
            # Same content, skip edit
            return
        else:
            # Same window, text changed - edit in place
            # Send typing indicator when Codex is working
            if "esc to interrupt" in status_text.lower():
                try:
                    await bot.send_chat_action(
                        chat_id=chat_id, action=ChatAction.TYPING
                    )
                except RetryAfter:
                    raise
                except Exception:
                    pass
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=_ensure_formatted(status_text),
                    parse_mode=PARSE_MODE,
                    link_preview_options=NO_LINK_PREVIEW,
                )
                _status_msg_info[skey] = (msg_id, wid, status_text)
            except RetryAfter:
                raise
            except Exception:
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=msg_id,
                        text=status_text,
                        link_preview_options=NO_LINK_PREVIEW,
                    )
                    _status_msg_info[skey] = (msg_id, wid, status_text)
                except RetryAfter:
                    raise
                except Exception as e:
                    logger.debug(f"Failed to edit status message: {e}")
                    _status_msg_info.pop(skey, None)
                    await _do_send_status_message(bot, user_id, tid, wid, status_text)
    else:
        # No existing status message, send new
        await _do_send_status_message(bot, user_id, tid, wid, status_text)


async def _do_send_status_message(
    bot: Bot,
    user_id: int,
    thread_id_or_0: int,
    window_id: str,
    text: str,
) -> None:
    """Send a new status message and track it (internal, called from worker)."""
    skey = (user_id, thread_id_or_0)
    thread_id: int | None = thread_id_or_0 if thread_id_or_0 != 0 else None
    chat_id = session_manager.resolve_chat_id(user_id, thread_id)
    # Safety net: delete any orphaned status message before sending a new one.
    # This catches edge cases where tracking was cleared without deleting the message.
    old = _status_msg_info.pop(skey, None)
    if old:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=old[0])
        except Exception:
            pass
    # Send typing indicator when Codex is working
    if "esc to interrupt" in text.lower():
        try:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except RetryAfter:
            raise
        except Exception:
            pass
    sent = await send_with_fallback(
        bot,
        chat_id,
        text,
        **_send_kwargs(thread_id),  # type: ignore[arg-type]
    )
    if sent:
        _status_msg_info[skey] = (sent.message_id, window_id, text)


async def _do_clear_status_message(
    bot: Bot,
    user_id: int,
    thread_id_or_0: int = 0,
) -> None:
    """Delete the status message for a user (internal, called from worker)."""
    skey = (user_id, thread_id_or_0)
    info = _status_msg_info.pop(skey, None)
    if info:
        msg_id = info[0]
        chat_id = session_manager.resolve_chat_id(user_id, thread_id_or_0 or None)
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception as e:
            logger.debug(f"Failed to delete status message {msg_id}: {e}")


async def _check_and_send_status(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
) -> None:
    """Check terminal for status line and send status message if present."""
    # Skip if there are more messages pending in the queue
    queue = _message_queues.get(_queue_key(user_id, thread_id))
    if queue and not queue.empty():
        return
    w = await tmux_manager.find_window_by_id(window_id)
    if not w:
        return

    pane_text = await tmux_manager.capture_pane(w.window_id)
    if not pane_text:
        return

    status_line = parse_status_line(pane_text)
    if status_line:
        await _process_status_update_task(
            bot,
            user_id,
            MessageTask(
                task_type="status_update",
                text=status_line,
                window_id=window_id,
                thread_id=thread_id,
            ),
        )


async def enqueue_content_message(
    bot: Bot,
    user_id: int,
    window_id: str,
    parts: list[str],
    tool_use_id: str | None = None,
    content_type: str = "text",
    text: str | None = None,
    thread_id: int | None = None,
    image_data: list[tuple[str, bytes]] | None = None,
    convert_status_to_content: bool = True,
) -> None:
    """Enqueue a content message task."""
    logger.debug(
        "Enqueue content: user=%d, window_id=%s, content_type=%s",
        user_id,
        window_id,
        content_type,
    )
    queue = get_or_create_queue(bot, user_id, thread_id)

    task = MessageTask(
        task_type="content",
        text=text,
        window_id=window_id,
        parts=parts,
        tool_use_id=tool_use_id,
        content_type=content_type,
        thread_id=thread_id,
        image_data=image_data,
        convert_status_to_content=convert_status_to_content,
    )
    _record_completion_diagnostic_event(
        user_id=user_id,
        thread_id_or_0=_queue_key(user_id, thread_id)[1],
        event="accepted",
        task_type="content",
        window_id=window_id,
        session_id=None,
        turn_id=None,
        queue_attempt=0,
        reason="content_enqueued",
    )
    await _enqueue_with_pressure_policy(
        queue,
        _queue_locks[_queue_key(user_id, thread_id)],
        task,
        user_id,
        thread_id,
    )


async def enqueue_completion_message(
    bot: Bot,
    user_id: int,
    window_id: str,
    completion_text: str,
    session_id: str | None = None,
    turn_id: int | None = None,
    thread_id: int | None = None,
) -> None:
    """Enqueue a synthetic completion notification message.

    Completion messages are sent as normal content tasks so they remain ordered
    with regular Codex output and can replace an in-progress status message.
    """
    if not completion_text.strip():
        return

    queue = get_or_create_queue(bot, user_id, thread_id)

    task = MessageTask(
        task_type="completion",
        text=completion_text,
        window_id=window_id,
        parts=[completion_text],
        session_id=session_id,
        turn_id=turn_id,
        content_type="completion",
        thread_id=thread_id,
        convert_status_to_content=False,
    )
    _record_completion_diagnostic_event(
        user_id=user_id,
        thread_id_or_0=_queue_key(user_id, thread_id)[1],
        event="accepted",
        task_type="completion",
        window_id=window_id,
        session_id=session_id,
        turn_id=turn_id,
        queue_attempt=0,
        reason="completion_enqueued",
    )
    await _enqueue_with_pressure_policy(
        queue,
        _queue_locks[_queue_key(user_id, thread_id)],
        task,
        user_id,
        thread_id,
    )


async def enqueue_status_update(
    bot: Bot,
    user_id: int,
    window_id: str,
    status_text: str | None,
    thread_id: int | None = None,
) -> None:
    """Enqueue status update. Skipped if text unchanged or during flood control."""
    # Don't enqueue during flood control — they'd just be dropped
    flood_end = _flood_until.get(_queue_key(user_id, thread_id), 0)
    if flood_end > time.monotonic():
        return

    tid = thread_id or 0

    # Deduplicate: skip if text matches what's already displayed
    if status_text:
        skey = (user_id, tid)
        info = _status_msg_info.get(skey)
        if info and info[1] == window_id and info[2] == status_text:
            return

    queue = get_or_create_queue(bot, user_id, thread_id)

    if status_text:
        task = MessageTask(
            task_type="status_update",
            text=status_text,
            window_id=window_id,
            thread_id=thread_id,
        )
    else:
        task = MessageTask(task_type="status_clear", thread_id=thread_id)

    await _enqueue_with_pressure_policy(
        queue,
        _queue_locks[_queue_key(user_id, thread_id)],
        task,
        user_id,
        thread_id,
    )


def clear_status_msg_info(user_id: int, thread_id: int | None = None) -> None:
    """Clear status message tracking for a user (and optionally a specific thread)."""
    skey = (user_id, thread_id or 0)
    _status_msg_info.pop(skey, None)


def clear_tool_msg_ids_for_topic(user_id: int, thread_id: int | None = None) -> None:
    """Clear tool message ID tracking for a specific topic.

    Removes all entries in _tool_msg_ids that match the given user and thread.
    """
    tid = thread_id or 0
    # Find and remove all matching keys
    keys_to_remove = [
        key for key in _tool_msg_ids if key[1] == user_id and key[2] == tid
    ]
    for key in keys_to_remove:
        _tool_msg_ids.pop(key, None)


async def shutdown_workers() -> None:
    """Stop all queue workers (called during bot shutdown)."""
    for _, worker in list(_queue_workers.items()):
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
    _queue_workers.clear()
    _message_queues.clear()
    _queue_locks.clear()
    _completion_pending_turns.clear()
    _completion_complete_turns.clear()
    _completion_diagnostic_events.clear()
    _status_drop_counts.clear()
    logger.info("Message queue workers stopped")
