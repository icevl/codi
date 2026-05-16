"""Telegram bot handlers — the main UI layer of CodexBot.

Registers all command/callback/message handlers and manages the bot lifecycle.
Each Telegram topic maps 1:1 to a tmux window (Codex session).

Core responsibilities:
  - Command handlers: /start, /history, /screenshot, /esc, /kill, /unbind,
    plus forwarding unknown /commands to Codex via tmux.
  - Callback query handler: directory browser, history pagination,
    interactive UI navigation, screenshot refresh.
  - Topic-based routing: each named topic binds to one tmux window.
    Unbound topics trigger the directory browser to create a new session.
  - Photo handling: photos sent by user are downloaded and forwarded
    to Codex as file paths (photo_handler).
  - Voice handling: voice messages are transcribed via OpenAI API and
    forwarded as text (voice_handler).
  - Automatic cleanup: closing a topic kills the associated window
    (topic_closed_handler). Unsupported content (stickers, etc.)
    is rejected with a warning (unsupported_content_handler).
  - Bot lifecycle management: post_init, post_shutdown, create_bot.

Handler modules (in handlers/):
  - callback_data: Callback data constants
  - message_queue: Per-user message queue management
  - message_sender: Safe message sending helpers
  - history: Message history pagination
  - directory_browser: Directory browser UI
  - interactive_ui: Interactive UI handling
  - status_polling: Terminal status polling
  - response_builder: Response message building

Key functions: create_bot(), handle_new_message().
"""

import asyncio
import io
import logging
import time
from pathlib import Path
from typing import Any

from PIL import Image
from telegram import (
    Bot,
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaDocument,
    InputMediaPhoto,
    Update,
)
from telegram.error import BadRequest, NetworkError, TimedOut
from telegram.ext import (
    AIORateLimiter,
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import config
from .handlers.callback_data import (
    CB_ASK_DOWN,
    CB_ASK_ENTER,
    CB_ASK_ESC,
    CB_ASK_LEFT,
    CB_ASK_PGDN,
    CB_ASK_PGUP,
    CB_PROMPT_CANCEL,
    CB_PROMPT_PAGE,
    CB_PROMPT_SELECT,
    CB_ASK_REFRESH,
    CB_ASK_RIGHT,
    CB_ASK_SPACE,
    CB_ASK_TAB,
    CB_ASK_UP,
    CB_DIR_CANCEL,
    CB_DIR_CONFIRM,
    CB_DIR_PAGE,
    CB_DIR_SELECT,
    CB_DIR_UP,
    CB_HISTORY_NEXT,
    CB_HISTORY_PREV,
    CB_SKILL_CANCEL,
    CB_SKILL_PAGE,
    CB_SKILL_SELECT,
    CB_SESSION_CANCEL,
    CB_SESSION_NEW,
    CB_SESSION_SELECT,
    CB_KEYS_PREFIX,
    CB_RUNTIME_CANCEL,
    CB_RUNTIME_CLAUDE,
    CB_RUNTIME_CODEX,
    CB_SCREENSHOT_REFRESH,
    CB_WIN_BIND,
    CB_WIN_CANCEL,
    CB_WIN_NEW,
)
from .handlers.directory_browser import (
    BROWSE_DIRS_KEY,
    BROWSE_PAGE_KEY,
    BROWSE_PATH_KEY,
    PENDING_RUNTIME_KEY,
    SESSIONS_KEY,
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
    STATE_SELECTING_RUNTIME,
    STATE_SELECTING_SESSION,
    STATE_SELECTING_WINDOW,
    UNBOUND_WINDOWS_KEY,
    build_directory_browser,
    build_runtime_picker,
    build_session_picker,
    build_window_picker,
    clear_browse_state,
    clear_runtime_picker_state,
    clear_session_picker_state,
    clear_window_picker_state,
)
from .runtimes import get_runtime
from .handlers.cleanup import clear_topic_state
from .handlers.history import send_history
from .handlers.interactive_ui import (
    clear_interactive_mode,
    clear_interactive_msg,
    get_interactive_msg_id,
    get_interactive_window,
    handle_interactive_ui,
    is_interactive_tool_name,
    render_interactive_message,
    set_interactive_mode,
)
from .handlers.message_queue import (
    clear_status_msg_info,
    enqueue_content_message,
    enqueue_completion_message,
    enqueue_status_update,
    format_diagnostic_events,
    get_queue_health,
    get_message_queue,
    get_diagnostic_events,
    shutdown_workers,
)
from .handlers.message_sender import (
    NO_LINK_PREVIEW,
    safe_edit,
    safe_reply,
    safe_send,
    send_with_fallback,
)
from .markdown_v2 import convert_markdown
from .handlers.response_builder import build_response_parts
from .handlers.status_polling import get_status_poll_heartbeat_age, status_poll_loop
from .screenshot import text_to_image
from .session import session_manager
from .session_monitor import (
    NewMessage,
    SessionMonitor,
    get_session_monitor_heartbeat_age,
)
from .skills import discover_skills, skill_invocation_prefix
from .terminal_parser import extract_bash_output, is_interactive_ui, parse_status_line
from .tmux_manager import tmux_manager
from .transcribe import close_client as close_transcribe_client
from .transcribe import transcribe_voice
from .utils import codexbot_dir

logger = logging.getLogger(__name__)

# Session monitor instance
session_monitor: SessionMonitor | None = None

# Status polling task
_status_poll_task: asyncio.Task | None = None

SCREENSHOT_FONT_SIZE = 40
SCREENSHOT_MEDIA_NAME = "screenshot.png"
SHOT_MEDIA_PHOTO = "photo"
SHOT_MEDIA_DOCUMENT = "document"
SCREENSHOT_MEDIA_MODES_KEY = "_screenshot_media_modes"
SCREENSHOT_MAX_PHOTO_BYTES = 8 * 1024 * 1024
SCREENSHOT_MAX_PHOTO_PIXELS = 10_000_000
SCREENSHOT_MAX_PHOTO_DIMENSION = 10000
SCREENSHOT_FALLBACK_CAPTION = "Preview unavailable, sent as document."
SCREENSHOT_CAPTURE_SCROLLBACK_LINES = 160
UNKNOWN_SINGLE_TOKEN_FALLBACK = (
    "⚠ Unknown skill token.\n"
    "I could not match `{token}` to a known skill.\n\n"
    "Use `/skillhelp` to pick a skill, then send your message."
)
PHOTO_DIMENSION_ERROR = "Photo_invalid_dimensions"
_SCREENSHOT_RESAMPLE = Image.Resampling.LANCZOS
SCREENSHOT_PREVIEW_SHRINK_FACTOR = 0.9
SCREENSHOT_PREVIEW_MAX_ATTEMPTS = 8

# Codex commands shown in bot menu (forwarded via tmux)
CODEX_COMMANDS: dict[str, str] = {
    "clear": "↗ Clear conversation history",
    "compact": "↗ Compact conversation context",
    "cost": "↗ Show token/cost usage",
    "help": "↗ Show Codex help",
    "memory": "↗ Edit AGENTS.md",
    "model": "↗ Switch AI model",
    "plan": "↗ Draft/update plan",
    "skills": "↗ List Codex skills",
}

PROGRESS_CONTENT_TYPES = {"thinking", "tool_use", "tool_result", "local_command"}
CLAUDE_HIDDEN_PROGRESS_CONTENT_TYPES = {"tool_result", "local_command"}
PROGRESS_MAX_LENGTH = 3000

# Skill board state keys
SKILLS_PER_PAGE = 8
SKILL_LIST_KEY = "skill_list"
SKILL_PAGE_KEY = "skill_page"
SKILL_THREAD_KEY = "skill_thread_id"
ARMED_SKILLS_KEY = "armed_skills"

# Structured interactive prompt state (request_user_input / AskUserQuestion)
INTERACTIVE_PROMPT_PAGE_SIZE = 6
_interactive_prompt_state: dict[tuple[int, int], dict[str, Any]] = {}
STRUCTURED_PROMPT_MODE_SINGLE_REQUEST = "single_request_user_input"
_pending_post_completion_interactive: dict[tuple[int, int], dict[str, Any]] = {}


def is_user_allowed(user_id: int | None) -> bool:
    return user_id is not None and config.is_user_allowed(user_id)


def _get_thread_id(update: Update) -> int | None:
    """Extract thread_id from an update, returning None if not in a named topic."""
    msg = update.message or (
        update.callback_query.message if update.callback_query else None
    )
    if msg is None:
        return None
    tid = getattr(msg, "message_thread_id", None)
    if tid is None or tid == 1:
        return None
    return tid


def _log_dispatch_event(
    *,
    event: str,
    user_id: int | None,
    thread_id: int | None,
    window_id: str | None = None,
    session_id: str | None = None,
    turn_id: int | None = None,
    message_len: int | None = None,
    is_complete: bool | None = None,
    skill_token: str | None = None,
    reason: str | None = None,
    error: bool = False,
) -> None:
    """Emit structured dispatch diagnostics without sending raw user text."""
    payload = {
        "event": event,
        "reason": reason,
        "user_id": user_id,
        "thread_id": thread_id,
        "window_id": window_id,
        "session_id": session_id,
        "turn_id": turn_id,
        "message_len": message_len,
        "is_complete": is_complete,
        "skill_token": skill_token,
    }
    line = "dispatch " + " ".join(f"{k}={v}" for k, v in payload.items())
    if error:
        logger.error(line)
    else:
        logger.warning(line)


def _get_armed_skills(user_data: dict | None) -> dict[int, str]:
    """Get mutable thread_id -> skill_name map from user_data."""
    if user_data is None:
        return {}
    armed = user_data.get(ARMED_SKILLS_KEY)
    if not isinstance(armed, dict):
        armed = {}
        user_data[ARMED_SKILLS_KEY] = armed
    return armed


def _get_armed_skill(user_data: dict | None, thread_id: int | None) -> str | None:
    """Get currently armed skill for a thread, if any."""
    if user_data is None or thread_id is None:
        return None
    armed = user_data.get(ARMED_SKILLS_KEY)
    if not isinstance(armed, dict):
        return None
    skill = armed.get(thread_id)
    return skill if isinstance(skill, str) and skill else None


def _clear_armed_skill(user_data: dict | None, thread_id: int | None) -> None:
    """Clear armed skill for a thread."""
    if user_data is None or thread_id is None:
        return
    armed = user_data.get(ARMED_SKILLS_KEY)
    if isinstance(armed, dict):
        armed.pop(thread_id, None)


def _apply_armed_skill(
    armed_skill: str | None, text: str, runtime: str | None = None
) -> str:
    if not armed_skill:
        return text
    prefix = skill_invocation_prefix(runtime)
    if text.strip():
        return f"{prefix}{armed_skill} {text}"
    return f"{prefix}{armed_skill}"


def _normalize_text_for_dispatch(
    text: str,
    armed_skill: str | None,
    discovered_skills: list[str] | None = None,
    runtime: str | None = None,
) -> tuple[str | None, str | None]:
    """Return normalized text and optional malformed single-token marker.

    Returns:
        (text_to_send, unknown_single_token)

    Unarmed single-token messages in the form ``$skill`` (Codex) or
    ``/skill`` (Claude) are forwarded. Bare ``$`` is rejected as malformed
    for Codex; ``/`` is handled by Telegram's command router before reaching
    this function.
    """
    _ = discovered_skills  # Backward-compatible signature; no discovery gate.
    if armed_skill:
        return _apply_armed_skill(armed_skill, text, runtime=runtime), None

    # Only the Codex $-prefix has the legacy "reject bare token" rule.
    # Claude's /-prefix is intercepted by the slash-command forwarder and
    # never reaches text_handler.
    if isinstance(runtime, str) and runtime == "claude":
        return text, None

    stripped = text.strip()
    if not stripped or len(stripped.split()) != 1:
        return text, None

    if not stripped.startswith("$"):
        return text, None

    skill_name = stripped[1:]
    if not skill_name:
        return None, stripped

    return f"${skill_name}", None


def _unknown_single_token_message(token: str) -> str:
    return UNKNOWN_SINGLE_TOKEN_FALLBACK.format(token=token)


def _is_stale_callback_query_error(exc: BadRequest) -> bool:
    text = str(exc).lower()
    return "query is too old" in text or "query id is invalid" in text


async def _safe_answer_callback_query(
    query: object,
    text: str | None = None,
    *,
    show_alert: bool = False,
) -> bool:
    """Answer a Telegram callback query, ignoring expiry races."""
    answer = getattr(query, "answer", None)
    if answer is None:
        return False
    try:
        await answer(text, show_alert=show_alert)
        return True
    except BadRequest as exc:
        if _is_stale_callback_query_error(exc):
            logger.info("Ignoring stale callback query answer: %s", exc)
            return False
        raise


def _build_progress_text(
    *,
    content_type: str,
    raw_text: str,
    parts: list[str],
    tool_name: str | None = None,
) -> str:
    text = "\n\n".join(part.strip() for part in parts if part and part.strip()).strip()
    if not text:
        text = raw_text.strip()
    if not text:
        if content_type == "tool_use" and tool_name:
            text = f"**{tool_name}**"
        elif content_type == "tool_result":
            text = "Tool result received."
        elif content_type == "thinking":
            text = "∴ Thinking…"
        elif content_type == "local_command":
            text = "Running local command…"
        else:
            text = "Working…"
    if len(text) > PROGRESS_MAX_LENGTH:
        text = text[: PROGRESS_MAX_LENGTH - 1].rstrip() + "…"
    return text


def _should_skip_progress_message(runtime_name: str | None, content_type: str) -> bool:
    """Hide Claude's noisy command output while keeping agent replies visible."""
    return (
        runtime_name == "claude"
        and content_type in CLAUDE_HIDDEN_PROGRESS_CONTENT_TYPES
    )


def _clear_callback_thread_state(
    user_data: dict[str, Any] | None, thread_id: int | None
) -> None:
    """Clear stale lifecycle state for a topic-specific callback."""
    if not user_data or thread_id is None:
        return

    if user_data.get("_pending_thread_id") == thread_id:
        clear_browse_state(user_data)
        clear_window_picker_state(user_data)
        clear_session_picker_state(user_data)
        user_data.pop("_pending_thread_id", None)
        user_data.pop("_pending_thread_text", None)


async def _guard_callback_window_match(
    *,
    query: Any,
    user_id: int,
    thread_id: int | None,
    window_id: str,
    user_data: dict[str, Any] | None,
    reason: str = "This action is stale.",
) -> bool:
    """Ensure callback thread/window mapping is current before mutating terminal state."""
    if not session_manager.is_window_bound_to_thread(user_id, thread_id, window_id):
        _clear_callback_thread_state(user_data, thread_id)
        await _safe_answer_callback_query(query, reason, show_alert=True)
        return False
    return True


async def _drain_thread_queue(user_id: int, thread_id: int | None) -> None:
    """Drain the current thread's queue before mutating terminal state."""
    queue = get_message_queue(user_id, thread_id)
    if queue is not None:
        await queue.join()


def _interactive_prompt_key(user_id: int, thread_id: int | None) -> tuple[int, int]:
    return (user_id, thread_id or 0)


def _get_structured_prompt_state(
    user_id: int,
    thread_id: int | None,
) -> dict[str, Any] | None:
    return _interactive_prompt_state.get(_interactive_prompt_key(user_id, thread_id))


def _set_structured_prompt_state(
    user_id: int,
    thread_id: int | None,
    state: dict[str, Any],
) -> None:
    _interactive_prompt_state[_interactive_prompt_key(user_id, thread_id)] = state


def _set_pending_post_completion_interactive(
    user_id: int,
    thread_id: int | None,
    state: dict[str, Any],
) -> None:
    _pending_post_completion_interactive[
        _interactive_prompt_key(user_id, thread_id)
    ] = state


def _pop_pending_post_completion_interactive(
    user_id: int,
    thread_id: int | None,
) -> dict[str, Any] | None:
    return _pending_post_completion_interactive.pop(
        _interactive_prompt_key(user_id, thread_id),
        None,
    )


def _normalize_prompt_options(raw_options: Any) -> list[dict[str, str]]:
    if not isinstance(raw_options, list):
        return []
    options: list[dict[str, str]] = []
    for item in raw_options:
        label = ""
        description = ""
        if isinstance(item, str):
            label = item.strip()
        elif isinstance(item, dict):
            for key in ("label", "title", "name", "value", "text"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    label = value.strip()
                    break
            desc_value = item.get("description")
            if isinstance(desc_value, str):
                description = desc_value.strip()
        if label:
            options.append({"label": label, "description": description})
    return options


def _extract_structured_prompt(
    tool_name: str | None, tool_input: Any
) -> dict[str, Any] | None:
    if tool_name != "request_user_input":
        return None
    if not isinstance(tool_input, dict):
        return None

    question_text = ""
    raw_options: Any = None

    questions = tool_input.get("questions")
    if isinstance(questions, list):
        if len(questions) != 1:
            return None
        first = questions[0]
        if isinstance(first, dict):
            if any(
                bool(first.get(key))
                for key in ("isOther", "is_other", "isSecret", "is_secret")
            ):
                return None
            q = first.get("question")
            if isinstance(q, str):
                question_text = q.strip()
            raw_options = first.get("options")
        else:
            return None

    if not question_text:
        q = tool_input.get("question")
        if isinstance(q, str):
            question_text = q.strip()

    if raw_options is None:
        raw_options = tool_input.get("options")
    if raw_options is None:
        raw_options = tool_input.get("choices")
    if raw_options is None:
        raw_options = tool_input.get("items")

    options = _normalize_prompt_options(raw_options)
    if not question_text or not options:
        return None

    return {
        "mode": STRUCTURED_PROMPT_MODE_SINGLE_REQUEST,
        "question": question_text,
        "options": options,
    }


def _is_supported_structured_prompt_state(prompt_state: dict[str, Any] | None) -> bool:
    if not isinstance(prompt_state, dict):
        return False
    return prompt_state.get("mode") == STRUCTURED_PROMPT_MODE_SINGLE_REQUEST


def _should_defer_interactive_prompt_until_completion(msg: NewMessage) -> bool:
    if msg.tool_name not in ("ExitPlanMode", "exit_plan_mode"):
        return False
    if not isinstance(msg.tool_input, dict):
        return False
    return bool(msg.tool_input.get("_defer_until_completion"))


def _build_interactive_tool_fallback_text(msg: NewMessage) -> str:
    if msg.tool_name in ("AskUserQuestion", "request_user_input"):
        questions = (
            msg.tool_input.get("questions", [])
            if isinstance(msg.tool_input, dict)
            else []
        )
        lines = [
            "Codex is waiting for your answer.",
            "Use the controls below to navigate the terminal prompt.",
        ]
        if isinstance(questions, list) and len(questions) > 1:
            lines.append("Use Tab to switch questions before pressing Enter.")
        return "\n".join(lines)

    if msg.tool_name in ("ExitPlanMode", "exit_plan_mode"):
        return "\n".join(
            [
                "Codex is waiting for a plan decision.",
                "1. Yes, implement this plan - Switch to Default and start coding.",
                "2. No, stay in Plan mode - Continue planning with the model.",
                "Use the controls below to choose and confirm it in the terminal.",
            ]
        )

    return "\n".join(
        [
            "Codex is waiting for input.",
            "Use the controls below to continue in the terminal.",
        ]
    )


def _build_structured_prompt_view(
    prompt_state: dict[str, Any],
    page: int,
) -> tuple[str, int, int, int, int]:
    options: list[dict[str, str]] = prompt_state.get("options", [])
    total_pages = max(
        1,
        (len(options) + INTERACTIVE_PROMPT_PAGE_SIZE - 1)
        // INTERACTIVE_PROMPT_PAGE_SIZE,
    )
    bounded_page = max(0, min(page, total_pages - 1))
    start = bounded_page * INTERACTIVE_PROMPT_PAGE_SIZE
    end = start + INTERACTIVE_PROMPT_PAGE_SIZE
    page_options = options[start:end]

    lines = [f"❓ {prompt_state.get('question', '').strip()}", ""]
    for idx, option in enumerate(page_options, start=start + 1):
        label = option.get("label", "")
        description = option.get("description", "")
        if description:
            lines.append(f"{idx}. {label} - {description}")
        else:
            lines.append(f"{idx}. {label}")

    lines.append("")
    if total_pages > 1:
        lines.append(f"Page {bounded_page + 1}/{total_pages}")
    lines.append("Tap an option button below.")

    return "\n".join(lines).strip(), bounded_page, total_pages, start, end


def _build_structured_prompt_keyboard(
    prompt_state: dict[str, Any],
    page: int,
) -> tuple[InlineKeyboardMarkup, int]:
    _text, bounded_page, total_pages, start, end = _build_structured_prompt_view(
        prompt_state,
        page,
    )
    options: list[dict[str, str]] = prompt_state.get("options", [])
    rows: list[list[InlineKeyboardButton]] = []
    for idx in range(start, min(end, len(options))):
        label = options[idx].get("label", "")
        button_label = label if len(label) <= 48 else f"{label[:47]}…"
        rows.append(
            [
                InlineKeyboardButton(
                    button_label,
                    callback_data=f"{CB_PROMPT_SELECT}{idx}",
                )
            ]
        )

    if total_pages > 1:
        nav: list[InlineKeyboardButton] = []
        if bounded_page > 0:
            nav.append(
                InlineKeyboardButton(
                    "◀",
                    callback_data=f"{CB_PROMPT_PAGE}{bounded_page - 1}",
                )
            )
        nav.append(
            InlineKeyboardButton(
                f"{bounded_page + 1}/{total_pages}", callback_data="noop"
            )
        )
        if bounded_page < total_pages - 1:
            nav.append(
                InlineKeyboardButton(
                    "▶",
                    callback_data=f"{CB_PROMPT_PAGE}{bounded_page + 1}",
                )
            )
        rows.append(nav)

    rows.append([InlineKeyboardButton("Cancel", callback_data=CB_PROMPT_CANCEL)])
    return InlineKeyboardMarkup(rows), bounded_page


async def _clear_structured_prompt_state(
    user_id: int,
    thread_id: int | None,
    *,
    bot: Bot | None = None,
    delete_message: bool = True,
) -> None:
    key = _interactive_prompt_key(user_id, thread_id)
    state = _interactive_prompt_state.pop(key, None)
    if not state or not bot or not delete_message:
        return
    message_id = state.get("message_id")
    if not isinstance(message_id, int):
        return
    chat_id = session_manager.resolve_chat_id(user_id, thread_id)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


async def _render_structured_prompt(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
    prompt_state: dict[str, Any],
) -> bool:
    page = int(prompt_state.get("page", 0))
    text, bounded_page, _total_pages, _start, _end = _build_structured_prompt_view(
        prompt_state,
        page,
    )
    keyboard, bounded_page = _build_structured_prompt_keyboard(prompt_state, page)
    prompt_state["page"] = bounded_page

    chat_id = session_manager.resolve_chat_id(user_id, thread_id)
    thread_kwargs: dict[str, int] = {}
    if thread_id is not None:
        thread_kwargs["message_thread_id"] = thread_id

    existing_message_id = prompt_state.get("message_id")
    if isinstance(existing_message_id, int):
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=existing_message_id,
                text=text,
                reply_markup=keyboard,
                link_preview_options=NO_LINK_PREVIEW,
            )
            return True
        except Exception:
            prompt_state.pop("message_id", None)

    try:
        sent = await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=keyboard,
            link_preview_options=NO_LINK_PREVIEW,
            **thread_kwargs,  # type: ignore[arg-type]
        )
    except Exception as e:
        logger.warning(
            "Failed to send structured interactive prompt user=%d thread=%s: %s",
            user_id,
            thread_id,
            e,
        )
        return False
    prompt_state["message_id"] = sent.message_id
    return True


async def _try_handle_structured_interactive_prompt(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None,
    msg: NewMessage,
) -> bool:
    prompt = _extract_structured_prompt(msg.tool_name, msg.tool_input)
    if prompt is None:
        return False

    key = _interactive_prompt_key(user_id, thread_id)
    existing = _interactive_prompt_state.get(key, {})
    prompt_state: dict[str, Any] = {
        "window_id": window_id,
        "mode": prompt["mode"],
        "question": prompt["question"],
        "options": prompt["options"],
        "page": 0,
    }
    existing_message_id = existing.get("message_id")
    if isinstance(existing_message_id, int):
        prompt_state["message_id"] = existing_message_id

    _set_structured_prompt_state(user_id, thread_id, prompt_state)
    handled = await _render_structured_prompt(bot, user_id, thread_id, prompt_state)
    if not handled:
        _interactive_prompt_state.pop(key, None)
        return False

    if get_interactive_msg_id(user_id, thread_id):
        await clear_interactive_msg(user_id, bot, thread_id)
    set_interactive_mode(user_id, window_id, thread_id)
    return True


def _build_skill_board(
    skills: list[str],
    page: int = 0,
    *,
    armed_skill: str | None = None,
) -> tuple[str, InlineKeyboardMarkup, int]:
    """Build paginated inline keyboard for Codex skills."""
    total_pages = max(1, (len(skills) + SKILLS_PER_PAGE - 1) // SKILLS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))

    start = page * SKILLS_PER_PAGE
    end = start + SKILLS_PER_PAGE
    page_skills = skills[start:end]

    buttons: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(page_skills), 2):
        row: list[InlineKeyboardButton] = []
        for j in range(min(2, len(page_skills) - i)):
            idx = start + i + j
            skill_name = skills[idx]
            label = skill_name if len(skill_name) <= 20 else skill_name[:19] + "…"
            row.append(
                InlineKeyboardButton(
                    f"🧩 {label}",
                    callback_data=f"{CB_SKILL_SELECT}{idx}",
                )
            )
        buttons.append(row)

    if total_pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(
                InlineKeyboardButton("◀", callback_data=f"{CB_SKILL_PAGE}{page - 1}")
            )
        nav.append(
            InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop")
        )
        if page < total_pages - 1:
            nav.append(
                InlineKeyboardButton("▶", callback_data=f"{CB_SKILL_PAGE}{page + 1}")
            )
        buttons.append(nav)

    buttons.append(
        [InlineKeyboardButton("Cancel Armed Skill", callback_data=CB_SKILL_CANCEL)]
    )

    lines = [
        "🧩 *Codex Skills*",
        "",
        "Tap a skill. Your next message in this topic will be sent as:",
        "`$skill-name your message`",
    ]
    if armed_skill:
        lines.extend(["", f"*Armed for next message:* `{armed_skill}`"])
    text = "\n".join(lines)
    return text, InlineKeyboardMarkup(buttons), page


def _build_completion_text(msg: NewMessage) -> str:
    """Build completion notification text for a completed turn."""
    parts: list[str] = ["✅ *Codex turn complete*"]
    if msg.turn_id is not None:
        parts.append(f"Turn: `{msg.turn_id}`")

    if not msg.turn_had_visible_output:
        parts.append("*(no visible output)*")

    if msg.is_stale_turn:
        parts.append("⚠ This completion arrived after a newer turn started.")

    return "\n".join(parts)


def _build_screenshot_media(png_bytes: bytes) -> io.BytesIO:
    """Build a Telegram-ready screenshot media object."""
    media = io.BytesIO(png_bytes)
    media.name = SCREENSHOT_MEDIA_NAME
    return media


def _screenshot_mode_key(thread_id: int | None, window_id: str) -> str:
    """Build a stable screenshot mode key from topic/window scope."""
    thread_token = "none" if thread_id is None else str(thread_id)
    return f"{thread_token}:{window_id}"


def _get_screenshot_modes(user_data: dict | None) -> dict[str, str]:
    """Get mutable screenshot mode map from user_data."""
    if user_data is None:
        return {}
    modes = user_data.get(SCREENSHOT_MEDIA_MODES_KEY)
    if not isinstance(modes, dict):
        modes = {}
        user_data[SCREENSHOT_MEDIA_MODES_KEY] = modes
    return modes


def _set_screenshot_mode(
    user_data: dict | None,
    thread_id: int | None,
    window_id: str,
    mode: str,
) -> None:
    """Persist screenshot media mode for a topic/window pair."""
    if user_data is None:
        return
    _get_screenshot_modes(user_data)[_screenshot_mode_key(thread_id, window_id)] = mode


def _get_screenshot_mode(
    user_data: dict | None,
    thread_id: int | None,
    window_id: str,
) -> str:
    """Read screenshot media mode; defaults to photo-first behavior."""
    return _get_screenshot_modes(user_data).get(
        _screenshot_mode_key(thread_id, window_id),
        SHOT_MEDIA_PHOTO,
    )


def _is_photo_payload_safe(png_bytes: bytes) -> bool:
    """Backward-compatible bool guard."""
    return _assess_photo_payload(png_bytes)["is_safe"]


def _assess_photo_payload(png_bytes: bytes) -> dict[str, Any]:
    """Inspect screenshot payload safety with structured details."""
    width: int = 0
    height: int = 0
    result: dict[str, Any] = {
        "is_safe": False,
        "reason": "inspection_failed",
        "width": None,
        "height": None,
        "pixel_count": None,
    }

    if len(png_bytes) > SCREENSHOT_MAX_PHOTO_BYTES:
        result["reason"] = "exceeds_max_bytes"
        return result

    try:
        with Image.open(io.BytesIO(png_bytes)) as image:
            width, height = image.size
            result["width"] = width
            result["height"] = height
    except Exception:
        result["reason"] = "invalid_image"
        return result

    if width <= 0 or height <= 0:
        result["reason"] = "invalid_dimensions"
        return result

    if (
        width > SCREENSHOT_MAX_PHOTO_DIMENSION
        or height > SCREENSHOT_MAX_PHOTO_DIMENSION
    ):
        result["reason"] = "exceeds_max_dimension"
        return result

    pixel_count = width * height
    result["pixel_count"] = pixel_count
    if pixel_count > SCREENSHOT_MAX_PHOTO_PIXELS:
        result["reason"] = "exceeds_max_pixels"
        return result

    result["reason"] = "safe"
    result["is_safe"] = True
    return result


def _classify_screenshot_send_error(exc: BaseException) -> str:
    """Map screenshot-send/edit failures to stable reasons."""
    if isinstance(exc, (TimedOut, NetworkError)):
        return "telegram_network_ambiguous"
    message = str(getattr(exc, "message", exc))
    if PHOTO_DIMENSION_ERROR in message:
        return PHOTO_DIMENSION_ERROR
    return "photo_send_failed"


def _is_ambiguous_network_error(exc: BaseException) -> bool:
    """True when Telegram failed but the message may have been delivered."""
    return isinstance(exc, (TimedOut, NetworkError))


def _log_screenshot_event(
    *,
    event: str,
    window_id: str | None,
    thread_id: int | None,
    user_id: int | None = None,
    reason: str,
    attempted_mode: str,
    chosen_mode: str | None = None,
    skill_token: str | None = None,
    pixel_count: int | None = None,
    width: int | None = None,
    height: int | None = None,
    source_width: int | None = None,
    source_height: int | None = None,
    source_pixel_count: int | None = None,
    resized_preview: bool | None = None,
    error: bool = False,
) -> None:
    payload = {
        "event": event,
        "window_id": window_id,
        "thread_id": thread_id,
        "user_id": user_id,
        "reason": reason,
        "attempted_mode": attempted_mode,
        "chosen_mode": chosen_mode,
        "skill_token": skill_token,
        "width": width,
        "height": height,
        "pixel_count": pixel_count,
        "source_width": source_width,
        "source_height": source_height,
        "source_pixel_count": source_pixel_count,
        "resized_preview": resized_preview,
    }
    line = "screenshot " + " ".join(
        f"{key}={value}" for key, value in payload.items() if value is not None
    )
    if error:
        logger.error(line)
    else:
        logger.warning(line)


def _select_screenshot_mode(
    user_data: dict | None,
    thread_id: int | None,
    window_id: str,
    png_bytes: bytes,
) -> str:
    """Choose preferred mode based on remembered state and payload guard."""
    mode = _get_screenshot_mode(user_data, thread_id, window_id)
    if mode == SHOT_MEDIA_DOCUMENT:
        return SHOT_MEDIA_DOCUMENT
    safety = _assess_photo_payload(png_bytes)
    if safety["is_safe"]:
        return SHOT_MEDIA_PHOTO
    if safety["reason"] in {
        "exceeds_max_bytes",
        "exceeds_max_dimension",
        "exceeds_max_pixels",
    }:
        # Oversized payloads can often be recovered via preview downscaling.
        return SHOT_MEDIA_PHOTO
    return SHOT_MEDIA_DOCUMENT


def _prepare_screenshot_photo_preview(
    png_bytes: bytes,
) -> tuple[bytes, dict[str, Any]]:
    """Return a photo-safe screenshot payload, downscaling when needed."""
    source_safety = _assess_photo_payload(png_bytes)
    metadata: dict[str, Any] = {
        "resized": False,
        "reason": source_safety["reason"],
        "source_width": source_safety["width"],
        "source_height": source_safety["height"],
        "source_pixel_count": source_safety["pixel_count"],
    }
    if source_safety["is_safe"]:
        return png_bytes, metadata
    if source_safety["reason"] not in {
        "exceeds_max_bytes",
        "exceeds_max_dimension",
        "exceeds_max_pixels",
    }:
        return png_bytes, metadata

    try:
        with Image.open(io.BytesIO(png_bytes)) as image:
            source_width, source_height = image.size
            if source_width <= 0 or source_height <= 0:
                return png_bytes, metadata

            scale = 1.0
            max_side = max(source_width, source_height)
            if max_side > SCREENSHOT_MAX_PHOTO_DIMENSION:
                scale = min(scale, SCREENSHOT_MAX_PHOTO_DIMENSION / max_side)

            source_pixels = source_width * source_height
            if source_pixels > SCREENSHOT_MAX_PHOTO_PIXELS:
                scale = min(
                    scale,
                    (SCREENSHOT_MAX_PHOTO_PIXELS / source_pixels) ** 0.5,
                )

            if scale >= 1.0 and source_safety["reason"] == "exceeds_max_bytes":
                scale = SCREENSHOT_PREVIEW_SHRINK_FACTOR

            width = max(1, int(source_width * scale))
            height = max(1, int(source_height * scale))
            preview_image = image.resize((width, height), _SCREENSHOT_RESAMPLE)

            attempts = 0
            preview_bytes: bytes = b""
            preview_safety: dict[str, Any] = {"is_safe": False, "reason": "unknown"}
            while attempts < SCREENSHOT_PREVIEW_MAX_ATTEMPTS:
                buffer = io.BytesIO()
                preview_image.save(buffer, format="PNG", optimize=True)
                preview_bytes = buffer.getvalue()
                preview_safety = _assess_photo_payload(preview_bytes)
                if preview_safety["is_safe"]:
                    metadata.update(
                        {
                            "resized": True,
                            "reason": "resized_preview",
                            "source_width": source_width,
                            "source_height": source_height,
                            "source_pixel_count": source_pixels,
                        }
                    )
                    return preview_bytes, metadata

                if preview_safety["reason"] not in {
                    "exceeds_max_bytes",
                    "exceeds_max_dimension",
                    "exceeds_max_pixels",
                }:
                    break

                next_width = max(
                    1, int(preview_image.width * SCREENSHOT_PREVIEW_SHRINK_FACTOR)
                )
                next_height = max(
                    1, int(preview_image.height * SCREENSHOT_PREVIEW_SHRINK_FACTOR)
                )
                if (
                    next_width == preview_image.width
                    and next_height == preview_image.height
                ):
                    if preview_image.width == 1 and preview_image.height == 1:
                        break
                    next_width = max(1, preview_image.width - 1)
                    next_height = max(1, preview_image.height - 1)
                preview_image = image.resize(
                    (next_width, next_height), _SCREENSHOT_RESAMPLE
                )
                attempts += 1
    except Exception:
        return png_bytes, metadata

    return png_bytes, metadata
    return SHOT_MEDIA_PHOTO


def _alternate_screenshot_mode(mode: str) -> str:
    """Pick alternate mode when current screenshot media mode fails."""
    if mode == SHOT_MEDIA_DOCUMENT:
        return SHOT_MEDIA_PHOTO
    return SHOT_MEDIA_DOCUMENT


def _build_screenshot_input_media(png_bytes: bytes, mode: str) -> Any:
    """Build InputMedia payload matching screenshot mode."""
    media = _build_screenshot_media(png_bytes)
    if mode == SHOT_MEDIA_DOCUMENT:
        return InputMediaDocument(media=media)
    return InputMediaPhoto(media=media)


async def _send_screenshot_with_fallback(
    *,
    message: Any,
    user_data: dict | None,
    thread_id: int | None,
    window_id: str,
    png_bytes: bytes,
    keyboard: InlineKeyboardMarkup,
    user_id: int | None = None,
) -> str:
    """Send screenshot photo-first with document fallback and mode tracking."""
    preferred_mode = _select_screenshot_mode(user_data, thread_id, window_id, png_bytes)
    source_safety = _assess_photo_payload(png_bytes)
    photo_bytes, preview_meta = _prepare_screenshot_photo_preview(png_bytes)
    photo_safety = _assess_photo_payload(photo_bytes)

    if preferred_mode == SHOT_MEDIA_DOCUMENT:
        _log_screenshot_event(
            event="screenshot_send_selected_mode",
            user_id=user_id,
            thread_id=thread_id,
            window_id=window_id,
            reason=source_safety["reason"],
            attempted_mode=SHOT_MEDIA_DOCUMENT,
            chosen_mode=SHOT_MEDIA_DOCUMENT,
            width=source_safety["width"],
            height=source_safety["height"],
            pixel_count=source_safety["pixel_count"],
            resized_preview=False,
            skill_token="screenshot",
        )
        try:
            await message.reply_document(
                document=_build_screenshot_media(png_bytes),
                caption=SCREENSHOT_FALLBACK_CAPTION,
                reply_markup=keyboard,
            )
        except (TimedOut, NetworkError) as e:
            logger.warning(
                "Screenshot document upload ack failed for window %s; "
                "assuming delivered: %s",
                window_id,
                e,
            )
        _set_screenshot_mode(user_data, thread_id, window_id, SHOT_MEDIA_DOCUMENT)
        return SHOT_MEDIA_DOCUMENT

    if not photo_safety["is_safe"]:
        _log_screenshot_event(
            event="screenshot_send_selected_mode",
            user_id=user_id,
            thread_id=thread_id,
            window_id=window_id,
            reason=photo_safety["reason"],
            attempted_mode=SHOT_MEDIA_PHOTO,
            chosen_mode=SHOT_MEDIA_DOCUMENT,
            width=source_safety["width"],
            height=source_safety["height"],
            pixel_count=source_safety["pixel_count"],
            source_width=preview_meta["source_width"],
            source_height=preview_meta["source_height"],
            source_pixel_count=preview_meta["source_pixel_count"],
            resized_preview=bool(preview_meta["resized"]),
            skill_token="screenshot",
        )
        try:
            await message.reply_document(
                document=_build_screenshot_media(png_bytes),
                caption=SCREENSHOT_FALLBACK_CAPTION,
                reply_markup=keyboard,
            )
        except (TimedOut, NetworkError) as e:
            logger.warning(
                "Screenshot document upload ack failed for window %s; "
                "assuming delivered: %s",
                window_id,
                e,
            )
        _set_screenshot_mode(user_data, thread_id, window_id, SHOT_MEDIA_DOCUMENT)
        return SHOT_MEDIA_DOCUMENT

    try:
        _log_screenshot_event(
            event="screenshot_send_attempt",
            user_id=user_id,
            thread_id=thread_id,
            window_id=window_id,
            reason=preview_meta["reason"],
            attempted_mode=preferred_mode,
            width=photo_safety["width"],
            height=photo_safety["height"],
            pixel_count=photo_safety["pixel_count"],
            source_width=preview_meta["source_width"],
            source_height=preview_meta["source_height"],
            source_pixel_count=preview_meta["source_pixel_count"],
            resized_preview=bool(preview_meta["resized"]),
            skill_token="screenshot",
        )
        await message.reply_photo(
            photo=_build_screenshot_media(photo_bytes),
            caption=None,
            reply_markup=keyboard,
        )
        _set_screenshot_mode(user_data, thread_id, window_id, SHOT_MEDIA_PHOTO)
        return SHOT_MEDIA_PHOTO
    except Exception as e:
        if _is_ambiguous_network_error(e):
            logger.warning(
                "Screenshot photo upload ack failed for window %s; "
                "assuming delivered, skipping document fallback: %s",
                window_id,
                e,
            )
            _set_screenshot_mode(user_data, thread_id, window_id, SHOT_MEDIA_PHOTO)
            return SHOT_MEDIA_PHOTO

        failure_reason = _classify_screenshot_send_error(e)
        _log_screenshot_event(
            event="screenshot_send_fallback",
            user_id=user_id,
            thread_id=thread_id,
            window_id=window_id,
            reason=failure_reason,
            attempted_mode=preferred_mode,
            chosen_mode=SHOT_MEDIA_DOCUMENT,
            width=photo_safety["width"],
            height=photo_safety["height"],
            pixel_count=photo_safety["pixel_count"],
            source_width=preview_meta["source_width"],
            source_height=preview_meta["source_height"],
            source_pixel_count=preview_meta["source_pixel_count"],
            resized_preview=bool(preview_meta["resized"]),
            skill_token="screenshot",
            error=True,
        )
        try:
            await message.reply_document(
                document=_build_screenshot_media(png_bytes),
                caption=SCREENSHOT_FALLBACK_CAPTION,
                reply_markup=keyboard,
            )
        except (TimedOut, NetworkError) as doc_err:
            logger.warning(
                "Screenshot document fallback ack failed for window %s; "
                "assuming delivered: %s",
                window_id,
                doc_err,
            )
        _set_screenshot_mode(user_data, thread_id, window_id, SHOT_MEDIA_DOCUMENT)
        return SHOT_MEDIA_DOCUMENT


async def _capture_screenshot_text(window_id: str) -> str | None:
    """Capture pane with scrollback for wider screenshot field-of-view."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "capture-pane",
            "-e",
            "-p",
            "-S",
            f"-{SCREENSHOT_CAPTURE_SCROLLBACK_LINES}",
            "-t",
            window_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            text = stdout.decode("utf-8")
            if text:
                return text
        else:
            logger.warning(
                "Scrollback capture failed for window %s: %s",
                window_id,
                stderr.decode("utf-8"),
            )
    except Exception as e:
        logger.warning("Scrollback capture error for window %s: %s", window_id, e)

    return await tmux_manager.capture_pane(window_id, with_ansi=True)


async def _edit_screenshot_with_fallback(
    *,
    query: Any,
    user_data: dict | None,
    thread_id: int | None,
    window_id: str,
    png_bytes: bytes,
    keyboard: InlineKeyboardMarkup,
    user_id: int | None = None,
) -> str | None:
    """Edit screenshot media and retry once in alternate mode on failure."""
    current_mode = _select_screenshot_mode(user_data, thread_id, window_id, png_bytes)
    source_safety = _assess_photo_payload(png_bytes)
    photo_bytes, preview_meta = _prepare_screenshot_photo_preview(png_bytes)
    photo_safety = _assess_photo_payload(photo_bytes)

    mode_attempts: list[str] = [current_mode, _alternate_screenshot_mode(current_mode)]
    if current_mode == SHOT_MEDIA_PHOTO and not photo_safety["is_safe"]:
        mode_attempts = [SHOT_MEDIA_DOCUMENT]
    mode_attempts = list(dict.fromkeys(mode_attempts))

    for mode in mode_attempts:
        payload_bytes = photo_bytes if mode == SHOT_MEDIA_PHOTO else png_bytes
        safety = photo_safety if mode == SHOT_MEDIA_PHOTO else source_safety
        reason = (
            preview_meta["reason"]
            if mode == SHOT_MEDIA_PHOTO
            else source_safety["reason"]
        )
        try:
            _log_screenshot_event(
                event="screenshot_edit_attempt",
                user_id=user_id,
                thread_id=thread_id,
                window_id=window_id,
                reason=reason,
                attempted_mode=mode,
                skill_token="screenshot_refresh",
                width=safety["width"],
                height=safety["height"],
                pixel_count=safety["pixel_count"],
                source_width=preview_meta["source_width"],
                source_height=preview_meta["source_height"],
                source_pixel_count=preview_meta["source_pixel_count"],
                resized_preview=bool(preview_meta["resized"]),
            )
            await query.edit_message_media(
                media=_build_screenshot_input_media(payload_bytes, mode),
                reply_markup=keyboard,
            )
            _set_screenshot_mode(user_data, thread_id, window_id, mode)
            return mode
        except Exception as e:
            if _is_ambiguous_network_error(e):
                logger.warning(
                    "Screenshot edit ack failed for window %s in mode %s; "
                    "assuming delivered, skipping mode fallback: %s",
                    window_id,
                    mode,
                    e,
                )
                _set_screenshot_mode(user_data, thread_id, window_id, mode)
                return mode

            failure_reason = _classify_screenshot_send_error(e)
            _log_screenshot_event(
                event="screenshot_edit_fallback",
                user_id=user_id,
                thread_id=thread_id,
                window_id=window_id,
                reason=failure_reason,
                attempted_mode=mode,
                skill_token="screenshot_refresh",
                width=safety["width"],
                height=safety["height"],
                pixel_count=safety["pixel_count"],
                source_width=preview_meta["source_width"],
                source_height=preview_meta["source_height"],
                source_pixel_count=preview_meta["source_pixel_count"],
                resized_preview=bool(preview_meta["resized"]),
                chosen_mode=_alternate_screenshot_mode(mode),
                error=True,
            )

    _log_screenshot_event(
        event="screenshot_edit_failed",
        user_id=user_id,
        thread_id=thread_id,
        window_id=window_id,
        reason="fallback_exhausted",
        attempted_mode=current_mode,
        skill_token="screenshot_refresh",
        width=source_safety["width"],
        height=source_safety["height"],
        pixel_count=source_safety["pixel_count"],
        source_width=preview_meta["source_width"],
        source_height=preview_meta["source_height"],
        source_pixel_count=preview_meta["source_pixel_count"],
        resized_preview=bool(preview_meta["resized"]),
        chosen_mode=None,
        error=True,
    )
    return None


# --- Command handlers ---


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(update.message, "You are not authorized to use this bot.")
        return

    clear_browse_state(context.user_data)

    if update.message:
        await safe_reply(
            update.message,
            "🤖 *Codex Monitor*\n\n"
            "Each topic is a session. Create a new topic to start.",
        )


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show message history for the active session or bound thread."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    wid = session_manager.resolve_window_for_thread(user.id, thread_id)
    if not wid:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return

    await send_history(update.message, wid)


async def skillhelp_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show skill board and arm-next-message flow for Telegram topics."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return
    thread_id = _get_thread_id(update)
    if thread_id is None:
        await safe_reply(
            update.message,
            "❌ Please use `/skillhelp` inside a named topic.",
        )
        return

    wid = session_manager.get_window_for_thread(user.id, thread_id)
    bound_runtime = session_manager.get_window_state(wid).runtime if wid else None
    skills = discover_skills(runtime=bound_runtime)
    if not skills:
        skills_root = (
            "~/.claude/skills" if bound_runtime == "claude" else "~/.codex/skills"
        )
        await safe_reply(
            update.message,
            f"⚠ No local skills detected under `{skills_root}`.",
        )
        return

    armed_skill = _get_armed_skill(context.user_data, thread_id)
    text, keyboard, page = _build_skill_board(skills, page=0, armed_skill=armed_skill)

    if context.user_data is not None:
        context.user_data[SKILL_LIST_KEY] = skills
        context.user_data[SKILL_PAGE_KEY] = page
        context.user_data[SKILL_THREAD_KEY] = thread_id

    await safe_reply(update.message, text, reply_markup=keyboard)


async def diag_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show recent dispatch/completion diagnostics for the current topic."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    if thread_id is None:
        await safe_reply(update.message, "❌ Use /diag inside a named topic.")
        return

    wid = session_manager.resolve_window_for_thread(user.id, thread_id)
    if not wid:
        await safe_reply(
            update.message,
            "❌ No session bound to this topic. Send a message to start one.",
        )
        return

    session = await session_manager.resolve_session_for_window(wid)
    session_id = session.session_id if session else None
    raw_events = get_diagnostic_events(user.id, thread_id)
    compact = format_diagnostic_events(raw_events, max_events=8)
    queue_health = get_queue_health(user.id, thread_id)
    status_poll_age = get_status_poll_heartbeat_age()
    monitor_age = get_session_monitor_heartbeat_age()
    warn_reasons: list[str] = []

    worker_alive = bool(queue_health.get("worker_alive"))
    queue_depth = int(queue_health.get("depth", 0))
    queue_max = int(queue_health.get("maxsize", 0))
    dropped_status = int(queue_health.get("dropped_status", 0))
    flood_remaining = float(queue_health.get("flood_remaining_seconds", 0.0))

    if not worker_alive and queue_depth > 0:
        warn_reasons.append("queue_worker_dead")
    status_stale_after = max(config.status_poll_interval * 3.0, 5.0)
    if status_poll_age is None:
        warn_reasons.append("status_poll_no_heartbeat")
    elif status_poll_age > status_stale_after:
        warn_reasons.append("status_poll_stale")

    monitor_stale_after = max(config.monitor_poll_interval * 3.0, 6.0)
    if monitor_age is None:
        warn_reasons.append("session_monitor_no_heartbeat")
    elif monitor_age > monitor_stale_after:
        warn_reasons.append("session_monitor_stale")

    overall_health = "WARN" if warn_reasons else "OK"
    rows: list[str] = [
        f"window={wid}",
        f"thread={thread_id}",
        f"session={session_id or 'unresolved'}",
    ]
    if compact:
        rows.append("")
        rows.append("recent:")
        rows.extend(f"• {row}" for row in compact)
    else:
        rows.append("")
        rows.append("No recent completion events.")

    rows.append("")
    rows.append("health:")
    rows.append(f"overall={overall_health}")
    rows.append(
        f"queue={queue_depth}/{queue_max} worker_alive={worker_alive} "
        f"dropped_status={dropped_status}"
    )
    rows.append(f"flood_wait={flood_remaining:.1f}s")
    if status_poll_age is None:
        rows.append("status_poll_age=unavailable")
    else:
        rows.append(f"status_poll_age={status_poll_age:.1f}s")
    if monitor_age is None:
        rows.append("session_monitor_age=unavailable")
    else:
        rows.append(f"session_monitor_age={monitor_age:.1f}s")
    if warn_reasons:
        rows.append("warnings=" + ",".join(warn_reasons))

    await safe_reply(update.message, "\n".join(rows))


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show local bot/topic status without forwarding `/status` into Codex."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    if thread_id is None:
        await safe_reply(update.message, "❌ Use /status inside a named topic.")
        return

    wid = session_manager.resolve_window_for_thread(user.id, thread_id)
    if not wid:
        await safe_reply(
            update.message,
            "❌ No session bound to this topic. Send a message to start one.",
        )
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        await safe_reply(update.message, f"❌ Window '{display}' no longer exists.")
        return

    pane_text = await tmux_manager.capture_pane(w.window_id)
    session = await session_manager.resolve_session_for_window(wid)
    window_state = session_manager.get_window_state(wid)
    display = session_manager.get_display_name(wid)
    rows = [
        f"window={display} ({wid})",
        f"session={(session.session_id if session else window_state.session_id) or 'unresolved'}",
        f"cwd={w.cwd or window_state.cwd or 'unknown'}",
    ]

    if pane_text and is_interactive_ui(pane_text):
        rows.append("terminal=interactive_prompt")
    else:
        status_line = parse_status_line(pane_text or "")
        rows.append(f"terminal={status_line or 'idle'}")

    rows.append("")
    rows.append("`/status` is handled locally to avoid interrupting the Codex TUI.")
    rows.append("Use `/diag` for queue and monitor diagnostics.")
    await safe_reply(update.message, "\n".join(rows))


async def screenshot_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Capture the current tmux pane and send it as an image."""
    command_token = "/screenshot"
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    wid = session_manager.resolve_window_for_thread(user.id, thread_id)
    if not wid:
        _log_dispatch_event(
            event="dispatch_no_bound_window",
            user_id=user.id,
            thread_id=thread_id,
            reason="no_active_window_for_topic",
            skill_token=command_token,
            message_len=len(command_token),
            is_complete=False,
        )
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        _log_dispatch_event(
            event="dispatch_stale_binding",
            user_id=user.id,
            thread_id=thread_id,
            window_id=wid,
            reason="window_missing",
            skill_token=command_token,
            message_len=len(command_token),
            is_complete=False,
        )
        await safe_reply(update.message, f"❌ Window '{display}' no longer exists.")
        return

    text = await _capture_screenshot_text(w.window_id)
    if not text:
        await safe_reply(update.message, "❌ Failed to capture pane content.")
        return

    png_bytes = await text_to_image(
        text,
        font_size=SCREENSHOT_FONT_SIZE,
        with_ansi=True,
    )
    keyboard = _build_screenshot_keyboard(wid)
    try:
        await _send_screenshot_with_fallback(
            message=update.message,
            user_data=context.user_data,
            thread_id=thread_id,
            window_id=wid,
            png_bytes=png_bytes,
            keyboard=keyboard,
            user_id=user.id,
        )
    except Exception as e:
        logger.error("Failed to deliver screenshot for window %s: %s", wid, e)
        await safe_reply(update.message, "❌ Failed to deliver screenshot.")


async def unbind_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Unbind this topic from its Codex session without killing the window."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    if thread_id is None:
        await safe_reply(update.message, "❌ This command only works in a topic.")
        return

    wid = session_manager.get_window_for_thread(user.id, thread_id)
    if not wid:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return

    display = session_manager.get_display_name(wid)
    session_manager.unbind_thread(user.id, thread_id)
    await clear_topic_state(user.id, thread_id, context.bot, context.user_data)

    await safe_reply(
        update.message,
        f"✅ Topic unbound from window '{display}'.\n"
        "The Codex session is still running in tmux.\n"
        "Send a message to bind to a new session.",
    )


async def kill_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Kill the bound tmux window for this topic and delete the topic."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    if thread_id is None:
        await safe_reply(update.message, "❌ This command only works in a topic.")
        return

    chat = update.effective_chat
    if chat is not None:
        session_manager.set_group_chat_id(user.id, thread_id, chat.id)

    wid = session_manager.get_window_for_thread(user.id, thread_id)
    display = session_manager.get_display_name(wid) if wid else None
    window_state = "No tmux window was bound to this topic."

    if wid:
        w = await tmux_manager.find_window_by_id(wid)
        if w:
            killed = await tmux_manager.kill_window(w.window_id)
            if killed:
                window_state = f"Window '{display}' was killed."
                logger.info(
                    "Kill command: killed window %s (user=%d, thread=%d)",
                    display,
                    user.id,
                    thread_id,
                )
            else:
                window_state = f"Failed to kill window '{display}'."
                logger.warning(
                    "Kill command: failed to kill window %s (user=%d, thread=%d)",
                    display,
                    user.id,
                    thread_id,
                )
        else:
            window_state = f"Window '{display}' was already gone."
            logger.info(
                "Kill command: window %s already gone (user=%d, thread=%d)",
                display,
                user.id,
                thread_id,
            )

        session_manager.unbind_thread(user.id, thread_id)

    await clear_topic_state(user.id, thread_id, context.bot, context.user_data)

    if chat is None:
        await safe_reply(
            update.message,
            f"⚠ {window_state}\nChat context is unavailable, so the topic could not be deleted.",
        )
        return

    try:
        await context.bot.delete_forum_topic(
            chat_id=chat.id,
            message_thread_id=thread_id,
        )
    except Exception as e:
        logger.warning(
            "Kill command: failed to delete topic thread=%d user=%d: %s",
            thread_id,
            user.id,
            e,
        )
        await safe_reply(
            update.message,
            f"⚠ {window_state}\nFailed to delete the topic: {e}",
        )


async def esc_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send Escape key to interrupt Codex."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)
    wid = session_manager.resolve_window_for_thread(user.id, thread_id)
    if not wid:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        await safe_reply(update.message, f"❌ Window '{display}' no longer exists.")
        return

    # Send Escape control character (no enter)
    await tmux_manager.send_keys(w.window_id, "\x1b", enter=False)
    await safe_reply(update.message, "⎋ Sent Escape")


# --- Screenshot keyboard with quick control keys ---

# key_id → (tmux_key, enter, literal)
_KEYS_SEND_MAP: dict[str, tuple[str, bool, bool]] = {
    "up": ("Up", False, False),
    "dn": ("Down", False, False),
    "lt": ("Left", False, False),
    "rt": ("Right", False, False),
    "esc": ("Escape", False, False),
    "ent": ("Enter", False, False),
    "spc": ("Space", False, False),
    "tab": ("Tab", False, False),
    "cc": ("C-c", False, False),
}

# key_id → display label (shown in callback answer toast)
_KEY_LABELS: dict[str, str] = {
    "up": "↑",
    "dn": "↓",
    "lt": "←",
    "rt": "→",
    "esc": "⎋ Esc",
    "ent": "⏎ Enter",
    "spc": "␣ Space",
    "tab": "⇥ Tab",
    "cc": "^C",
}


def _build_screenshot_keyboard(window_id: str) -> InlineKeyboardMarkup:
    """Build inline keyboard for screenshot: control keys + refresh."""

    def btn(label: str, key_id: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            label,
            callback_data=f"{CB_KEYS_PREFIX}{key_id}:{window_id}"[:64],
        )

    return InlineKeyboardMarkup(
        [
            [btn("␣ Space", "spc"), btn("↑", "up"), btn("⇥ Tab", "tab")],
            [btn("←", "lt"), btn("↓", "dn"), btn("→", "rt")],
            [btn("⎋ Esc", "esc"), btn("^C", "cc"), btn("⏎ Enter", "ent")],
            [
                InlineKeyboardButton(
                    "🔄 Refresh",
                    callback_data=f"{CB_SCREENSHOT_REFRESH}{window_id}"[:64],
                )
            ],
        ]
    )


async def topic_closed_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle topic closure — kill the associated tmux window and clean up state."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return

    thread_id = _get_thread_id(update)
    if thread_id is None:
        return

    wid = session_manager.get_window_for_thread(user.id, thread_id)
    if wid:
        display = session_manager.get_display_name(wid)
        w = await tmux_manager.find_window_by_id(wid)
        if w:
            await tmux_manager.kill_window(w.window_id)
            logger.info(
                "Topic closed: killed window %s (user=%d, thread=%d)",
                display,
                user.id,
                thread_id,
            )
        else:
            logger.info(
                "Topic closed: window %s already gone (user=%d, thread=%d)",
                display,
                user.id,
                thread_id,
            )
        session_manager.unbind_thread(user.id, thread_id)
        # Clean up all memory state for this topic
        await clear_topic_state(user.id, thread_id, context.bot, context.user_data)
    else:
        logger.debug(
            "Topic closed: no binding (user=%d, thread=%d)", user.id, thread_id
        )


async def topic_edited_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle topic rename — sync new name to tmux window and internal state."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return

    msg = update.message
    if not msg or not msg.forum_topic_edited:
        return

    new_name = msg.forum_topic_edited.name
    if new_name is None:
        # Icon-only change, no rename needed
        return

    thread_id = _get_thread_id(update)
    if thread_id is None:
        return

    wid = session_manager.get_window_for_thread(user.id, thread_id)
    if not wid:
        logger.debug(
            "Topic edited: no binding (user=%d, thread=%d)", user.id, thread_id
        )
        return

    old_name = session_manager.get_display_name(wid)
    await tmux_manager.rename_window(wid, new_name)
    session_manager.update_display_name(wid, new_name)
    logger.info(
        "Topic renamed: '%s' -> '%s' (window=%s, user=%d, thread=%d)",
        old_name,
        new_name,
        wid,
        user.id,
        thread_id,
    )


async def forward_command_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Forward any non-bot command as a slash command to the active Codex session."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    thread_id = _get_thread_id(update)

    # Capture group chat_id for supergroup forum topic routing.
    # Required: Telegram Bot API needs group chat_id (not user_id) to send
    # messages with message_thread_id. Do NOT remove — see session.py docs.
    chat = update.effective_chat
    if chat and thread_id is not None:
        session_manager.set_group_chat_id(user.id, thread_id, chat.id)

    cmd_text = update.message.text or ""
    # The full text is already a slash command like "/clear" or "/compact foo"
    cc_slash = cmd_text.split("@")[0]  # strip bot mention
    wid = session_manager.resolve_window_for_thread(user.id, thread_id)
    if not wid:
        await safe_reply(update.message, "❌ No session bound to this topic.")
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        await safe_reply(update.message, f"❌ Window '{display}' no longer exists.")
        return

    display = session_manager.get_display_name(wid)
    logger.info(
        "Forwarding command %s to window %s (user=%d)", cc_slash, display, user.id
    )
    success, message = await session_manager.send_to_window(wid, cc_slash)
    if success:
        await safe_reply(update.message, f"⚡ [{display}] Sent: {cc_slash}")
        # If /clear command was sent, clear the session association
        # so we can detect the new session after first message
        if cc_slash.strip().lower() == "/clear":
            logger.info("Clearing session for window %s after /clear", display)
            session_manager.clear_window_session(wid)

        # Interactive commands (e.g. /model) render a terminal-based UI
        # with no JSONL tool_use entry.  The status poller already detects
        # interactive UIs every 1s (status_polling.py), so no
        # proactive detection needed here — the poller handles it.
    else:
        _log_dispatch_event(
            event="send_to_window_failed",
            user_id=user.id,
            thread_id=thread_id,
            window_id=wid,
            message_len=len(cc_slash),
            is_complete=False,
            reason=message,
            skill_token=cc_slash,
        )
        await safe_reply(update.message, f"❌ {message}")


async def unsupported_content_handler(
    update: Update,
    _context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Reply to non-text messages (stickers, video, etc.)."""
    if not update.message:
        return
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    logger.debug("Unsupported content from user %d", user.id)
    await safe_reply(
        update.message,
        "⚠ Only text, photo, and voice messages are supported. Stickers, video, and other media cannot be forwarded to Codex.",
    )


# --- Image directory for incoming photos ---
_IMAGES_DIR = codexbot_dir() / "images"
_IMAGES_DIR.mkdir(parents=True, exist_ok=True)


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photos sent by the user: download and forward path to Codex."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(update.message, "You are not authorized to use this bot.")
        return

    if not update.message or not update.message.photo:
        return

    chat = update.message.chat
    thread_id = _get_thread_id(update)
    if thread_id is not None:
        session_manager.set_group_chat_id(user.id, thread_id, chat.id)

    # Must be in a named topic
    if thread_id is None:
        await safe_reply(
            update.message,
            "❌ Please use a named topic. Create a new topic to start a session.",
        )
        return

    wid = session_manager.get_window_for_thread(user.id, thread_id)
    if wid is None:
        _log_dispatch_event(
            event="dispatch_no_bound_window",
            user_id=user.id,
            thread_id=thread_id,
            reason="no_active_window_for_topic",
            message_len=0,
            is_complete=False,
        )
        await safe_reply(
            update.message,
            "❌ No session bound to this topic. Send a text message first to create one.",
        )
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        session_manager.unbind_thread(user.id, thread_id)
        _log_dispatch_event(
            event="dispatch_stale_binding",
            user_id=user.id,
            thread_id=thread_id,
            window_id=wid,
            reason="window_missing",
            is_complete=False,
            message_len=0,
        )
        await safe_reply(
            update.message,
            f"❌ Window '{display}' no longer exists. Binding removed.\n"
            "Send a message to start a new session.",
        )
        return

    # Download the highest-resolution photo
    photo = update.message.photo[-1]
    tg_file = await photo.get_file()

    # Save to ~/.codexbot/images/<timestamp>_<file_unique_id>.jpg
    filename = f"{int(time.time())}_{photo.file_unique_id}.jpg"
    file_path = _IMAGES_DIR / filename
    await tg_file.download_to_drive(file_path)

    # Build the message to send to Codex
    caption = update.message.caption or ""
    if caption:
        text_to_send = f"{caption}\n\n(image attached: {file_path})"
    else:
        text_to_send = f"(image attached: {file_path})"

    clear_status_msg_info(user.id, thread_id)

    success, message = await session_manager.send_to_window(wid, text_to_send)
    if not success:
        _log_dispatch_event(
            event="send_to_window_failed",
            user_id=user.id,
            thread_id=thread_id,
            window_id=wid,
            message_len=len(text_to_send),
            is_complete=False,
            reason=message,
            skill_token="image_send",
        )
        await safe_reply(update.message, f"❌ {message}")
        return

    # Confirm to user
    await safe_reply(update.message, "📷 Image sent to Codex.")


async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle voice messages: transcribe via OpenAI and forward text to Codex."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(update.message, "You are not authorized to use this bot.")
        return

    if not update.message or not update.message.voice:
        return

    if not config.openai_api_key:
        await safe_reply(
            update.message,
            "⚠ Voice transcription requires an OpenAI API key.\n"
            "Set `OPENAI_API_KEY` in your `.env` file and restart the bot.",
        )
        return

    chat = update.message.chat
    thread_id = _get_thread_id(update)
    if thread_id is not None:
        session_manager.set_group_chat_id(user.id, thread_id, chat.id)

    if thread_id is None:
        await safe_reply(
            update.message,
            "❌ Please use a named topic. Create a new topic to start a session.",
        )
        return

    wid = session_manager.get_window_for_thread(user.id, thread_id)
    if wid is None:
        _log_dispatch_event(
            event="dispatch_no_bound_window",
            user_id=user.id,
            thread_id=thread_id,
            reason="no_active_window_for_topic",
            message_len=0,
            is_complete=False,
        )
        await safe_reply(
            update.message,
            "❌ No session bound to this topic. Send a text message first to create one.",
        )
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        session_manager.unbind_thread(user.id, thread_id)
        _log_dispatch_event(
            event="dispatch_stale_binding",
            user_id=user.id,
            thread_id=thread_id,
            window_id=wid,
            reason="window_missing",
            is_complete=False,
            message_len=0,
        )
        await safe_reply(
            update.message,
            f"❌ Window '{display}' no longer exists. Binding removed.\n"
            "Send a message to start a new session.",
        )
        return

    # Download voice as in-memory bytes
    voice_file = await update.message.voice.get_file()
    ogg_data = bytes(await voice_file.download_as_bytearray())

    # Transcribe
    try:
        text = await transcribe_voice(ogg_data)
    except ValueError as e:
        await safe_reply(update.message, f"⚠ {e}")
        return
    except Exception as e:
        logger.error("Voice transcription failed: %s", e)
        await safe_reply(update.message, f"⚠ Transcription failed: {e}")
        return

    clear_status_msg_info(user.id, thread_id)

    success, message = await session_manager.send_to_window(wid, text)
    if not success:
        _log_dispatch_event(
            event="send_to_window_failed",
            user_id=user.id,
            thread_id=thread_id,
            window_id=wid,
            message_len=len(text),
            is_complete=False,
            reason=message,
            skill_token="voice_message",
        )
        await safe_reply(update.message, f"❌ {message}")
        return

    await safe_reply(update.message, f'🎤 "{text}"')


# Active bash capture tasks: (user_id, thread_id) → asyncio.Task
_bash_capture_tasks: dict[tuple[int, int], asyncio.Task[None]] = {}


def _cancel_bash_capture(user_id: int, thread_id: int) -> None:
    """Cancel any running bash capture for this topic."""
    key = (user_id, thread_id)
    task = _bash_capture_tasks.pop(key, None)
    if task and not task.done():
        task.cancel()


async def _prime_attached_session_tracking(window_id: str) -> None:
    """Start monitoring an attached session from its current transcript end."""
    monitor = session_monitor
    if monitor is None:
        return

    session = await session_manager.resolve_session_for_window(window_id)
    if not session:
        return

    file_path = await session_manager.get_session_file_path(session.session_id)
    if not file_path:
        return

    try:
        offset = file_path.stat().st_size
    except OSError as exc:
        logger.debug(
            "attach_tracking_offset_unavailable window_id=%s session_id=%s error=%s",
            window_id,
            session.session_id,
            exc,
        )
        return

    monitor.set_initial_offset(session.session_id, offset)
    logger.info(
        "attach_tracking_offset_set window_id=%s session_id=%s offset=%d",
        window_id,
        session.session_id,
        offset,
    )


async def _capture_bash_output(
    bot: Bot,
    user_id: int,
    thread_id: int,
    window_id: str,
    command: str,
) -> None:
    """Background task: capture ``!`` bash command output from tmux pane.

    Sends the first captured output as a new message, then edits it
    in-place as more output appears.  Stops after 30 s or when cancelled
    (e.g. user sends a new message, which pushes content down).
    """
    try:
        # Wait for the command to start producing output
        await asyncio.sleep(2.0)

        chat_id = session_manager.resolve_chat_id(user_id, thread_id)
        msg_id: int | None = None
        last_output: str = ""

        for _ in range(30):
            raw = await tmux_manager.capture_pane(window_id)
            if raw is None:
                return

            output = extract_bash_output(raw, command)
            if not output:
                await asyncio.sleep(1.0)
                continue

            # Skip edit if nothing changed
            if output == last_output:
                await asyncio.sleep(1.0)
                continue

            last_output = output

            # Truncate to fit Telegram's 4096-char limit
            if len(output) > 3800:
                output = "… " + output[-3800:]

            if msg_id is None:
                # First capture — send a new message
                sent = await send_with_fallback(
                    bot,
                    chat_id,
                    output,
                    message_thread_id=thread_id,
                )
                if sent:
                    msg_id = sent.message_id
            else:
                # Subsequent captures — edit in place
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=msg_id,
                        text=convert_markdown(output),
                        parse_mode="MarkdownV2",
                        link_preview_options=NO_LINK_PREVIEW,
                    )
                except Exception:
                    try:
                        await bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=msg_id,
                            text=output,
                            link_preview_options=NO_LINK_PREVIEW,
                        )
                    except Exception:
                        pass

            await asyncio.sleep(1.0)
    except asyncio.CancelledError:
        return
    finally:
        _bash_capture_tasks.pop((user_id, thread_id), None)


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(update.message, "You are not authorized to use this bot.")
        return

    if not update.message or update.message.text is None:
        return

    thread_id = _get_thread_id(update)

    # Capture group chat_id for supergroup forum topic routing.
    # Required: Telegram Bot API needs group chat_id (not user_id) to send
    # messages with message_thread_id. Do NOT remove — see session.py docs.
    chat = update.effective_chat
    if chat and thread_id is not None:
        session_manager.set_group_chat_id(user.id, thread_id, chat.id)

    text = update.message.text

    # Ignore text in window picker mode (only for the same thread)
    if context.user_data and context.user_data.get(STATE_KEY) == STATE_SELECTING_WINDOW:
        pending_tid = context.user_data.get("_pending_thread_id")
        if pending_tid == thread_id:
            await safe_reply(
                update.message,
                "Please use the window picker above, or tap Cancel.",
            )
            return
        # Stale picker state from a different thread — clear it
        clear_window_picker_state(context.user_data)
        context.user_data.pop("_pending_thread_id", None)
        context.user_data.pop("_pending_thread_text", None)

    # Ignore text while the runtime picker is open (only for the same thread)
    if (
        context.user_data
        and context.user_data.get(STATE_KEY) == STATE_SELECTING_RUNTIME
    ):
        pending_tid = context.user_data.get("_pending_thread_id")
        if pending_tid == thread_id:
            await safe_reply(
                update.message,
                "Please pick an agent above, or tap Cancel.",
            )
            return
        clear_runtime_picker_state(context.user_data)
        context.user_data.pop("_pending_thread_id", None)
        context.user_data.pop("_pending_thread_text", None)
        context.user_data.pop(PENDING_RUNTIME_KEY, None)

    # Ignore text in directory browsing mode (only for the same thread)
    if (
        context.user_data
        and context.user_data.get(STATE_KEY) == STATE_BROWSING_DIRECTORY
    ):
        pending_tid = context.user_data.get("_pending_thread_id")
        if pending_tid == thread_id:
            await safe_reply(
                update.message,
                "Please use the directory browser above, or tap Cancel.",
            )
            return
        # Stale browsing state from a different thread — clear it
        clear_browse_state(context.user_data)
        context.user_data.pop("_pending_thread_id", None)
        context.user_data.pop("_pending_thread_text", None)

    # Ignore text in session picker mode (only for the same thread)
    if (
        context.user_data
        and context.user_data.get(STATE_KEY) == STATE_SELECTING_SESSION
    ):
        pending_tid = context.user_data.get("_pending_thread_id")
        if pending_tid == thread_id:
            await safe_reply(
                update.message,
                "Please use the session picker above, or tap Cancel.",
            )
            return
        # Stale picker state from a different thread — clear it
        clear_session_picker_state(context.user_data)
        context.user_data.pop("_pending_thread_id", None)
        context.user_data.pop("_pending_thread_text", None)
        context.user_data.pop("_selected_path", None)

    # Must be in a named topic
    if thread_id is None:
        await safe_reply(
            update.message,
            "❌ Please use a named topic. Create a new topic to start a session.",
        )
        return
    armed_skill = _get_armed_skill(context.user_data, thread_id)
    if not text and not armed_skill:
        return

    wid = session_manager.get_window_for_thread(user.id, thread_id)
    if wid is None:
        # Unbound topic — check for unbound windows first
        all_windows = await tmux_manager.list_windows()
        bound_ids = {wid for _, _, wid in session_manager.iter_thread_bindings()}
        unbound = [
            (w.window_id, w.window_name, w.cwd)
            for w in all_windows
            if w.window_id not in bound_ids
        ]
        logger.debug(
            "Window picker check: all=%s, bound=%s, unbound=%s",
            [w.window_name for w in all_windows],
            bound_ids,
            [name for _, name, _ in unbound],
        )

        if unbound:
            # Show window picker
            logger.info(
                "Unbound topic: showing window picker (%d unbound windows, user=%d, thread=%d)",
                len(unbound),
                user.id,
                thread_id,
            )
            msg_text, keyboard, win_ids = build_window_picker(unbound)
            if context.user_data is not None:
                context.user_data[STATE_KEY] = STATE_SELECTING_WINDOW
                context.user_data[UNBOUND_WINDOWS_KEY] = win_ids
                context.user_data["_pending_thread_id"] = thread_id
                context.user_data["_pending_thread_text"] = text
            await safe_reply(update.message, msg_text, reply_markup=keyboard)
            return

        # No unbound windows — ask for the agent runtime first, then
        # show the directory browser.
        logger.info(
            "Unbound topic: showing runtime picker (user=%d, thread=%d)",
            user.id,
            thread_id,
        )
        msg_text, keyboard = build_runtime_picker()
        if context.user_data is not None:
            context.user_data[STATE_KEY] = STATE_SELECTING_RUNTIME
            context.user_data["_pending_thread_id"] = thread_id
            context.user_data["_pending_thread_text"] = text
        await safe_reply(update.message, msg_text, reply_markup=keyboard)
        return

    # Bound topic — forward to bound window
    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        logger.info(
            "Stale binding: window %s gone, unbinding (user=%d, thread=%d)",
            display,
            user.id,
            thread_id,
        )
        session_manager.unbind_thread(user.id, thread_id)
        await safe_reply(
            update.message,
            f"❌ Window '{display}' no longer exists. Binding removed.\n"
            "Send a message to start a new session.",
        )
        return

    # Cancel any running bash capture — new message pushes pane content down
    _cancel_bash_capture(user.id, thread_id)

    bound_runtime = session_manager.get_window_state(wid).runtime

    # Check for pending interactive UI before sending text.
    # This catches UIs (permission prompts, etc.) that status polling might have missed.
    pane_text = await tmux_manager.capture_pane(w.window_id)
    if pane_text and is_interactive_ui(pane_text, runtime=bound_runtime):
        # UI detected — show it to user, then send text (acts as Enter)
        logger.info(
            "Detected pending interactive UI before sending text (user=%d, thread=%s)",
            user.id,
            thread_id,
        )
        await handle_interactive_ui(context.bot, user.id, wid, thread_id)
        # Small delay to let UI render in Telegram before text arrives
        await asyncio.sleep(0.3)

    stripped_text = text.strip()
    single_token_input = (
        stripped_text if stripped_text and len(stripped_text.split()) == 1 else None
    )
    text_to_send, unknown_single_token = _normalize_text_for_dispatch(
        text,
        armed_skill,
        runtime=bound_runtime,
    )
    if unknown_single_token is not None:
        _log_dispatch_event(
            event="dispatch_reject_unknown_single_token",
            user_id=user.id,
            thread_id=thread_id,
            window_id=wid,
            message_len=len(unknown_single_token),
            is_complete=False,
            skill_token=unknown_single_token,
            reason="unknown_single_token",
        )
        await safe_reply(
            update.message,
            _unknown_single_token_message(unknown_single_token),
        )
        return
    # `_normalize_text_for_dispatch` returns (str, None) on the happy path
    # and (None, str) on rejection — pyright can't see that correlation, so
    # narrow it here explicitly.
    assert text_to_send is not None
    normalized_single_token_skill = (
        single_token_input is not None
        and not armed_skill
        and text_to_send == f"${single_token_input}"
    )
    if normalized_single_token_skill:
        logger.debug(
            "Normalized single-token skill for dispatch in thread=%s user=%s window=%s: %s",
            thread_id,
            user.id,
            wid,
            text_to_send,
        )

    if armed_skill:
        logger.info(
            "Applying armed skill '%s' for user=%d thread=%d",
            armed_skill,
            user.id,
            thread_id,
        )

    # If the pane is currently showing an interactive prompt (AskUserQuestion,
    # Permission, RestoreCheckpoint, …) the TUI is in menu-mode and will not
    # accept free-text — letters get consumed as first-letter navigation and
    # the trailing Enter commits the wrong choice. Detect this lane and press
    # Escape first so the user's text lands in the regular agent input.
    interactive_before = get_interactive_window(user.id, thread_id)
    dismissed_interactive = False
    if interactive_before == wid:
        logger.info(
            "Dismissing interactive prompt before forwarding free text "
            "user=%d thread=%s window=%s",
            user.id,
            thread_id,
            wid,
        )
        await tmux_manager.send_keys(wid, "Escape", enter=False, literal=False)
        # Tiny gap so the TUI fully tears down the menu before we type.
        await asyncio.sleep(0.25)
        await clear_interactive_msg(user.id, context.bot, thread_id)
        dismissed_interactive = True

    success, message = await session_manager.send_to_window(wid, text_to_send)
    await enqueue_status_update(context.bot, user.id, wid, None, thread_id=thread_id)
    if normalized_single_token_skill:
        if success:
            logger.info(
                "Single-token dispatch sent successfully in thread=%s user=%s window=%s: %s",
                thread_id,
                user.id,
                wid,
                text_to_send,
            )
        else:
            _log_dispatch_event(
                event="send_to_window_failed",
                user_id=user.id,
                thread_id=thread_id,
                window_id=wid,
                message_len=len(text_to_send or ""),
                is_complete=False,
                skill_token=text_to_send if normalized_single_token_skill else None,
                reason=message,
            )
            logger.warning(
                "Single-token dispatch send failed in thread=%s user=%s window=%s: %s (%s)",
                thread_id,
                user.id,
                wid,
                text_to_send,
                message,
            )
    elif not success:
        _log_dispatch_event(
            event="send_to_window_failed",
            user_id=user.id,
            thread_id=thread_id,
            window_id=wid,
            message_len=len(text_to_send or ""),
            is_complete=False,
            reason=message,
        )
        logger.warning(
            "Failed to dispatch message in thread=%s user=%s window=%s: %s",
            thread_id,
            user.id,
            wid,
            message,
        )
    if not success:
        await safe_reply(update.message, f"❌ {message}")
        return

    if armed_skill:
        _clear_armed_skill(context.user_data, thread_id)

    if dismissed_interactive:
        try:
            await safe_reply(
                update.message,
                "⚠️ Interactive prompt dismissed (Esc) — your text was forwarded "
                "as a regular reply.",
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("notice send failed: %s", exc)

    # Start background capture for ! bash command output
    if text_to_send.startswith("!") and len(text_to_send) > 1:
        bash_cmd = text_to_send[1:]  # strip leading "!"
        task = asyncio.create_task(
            _capture_bash_output(context.bot, user.id, thread_id, wid, bash_cmd)
        )
        _bash_capture_tasks[(user.id, thread_id)] = task

    # If in interactive mode, refresh the UI after sending text
    interactive_window = get_interactive_window(user.id, thread_id)
    if interactive_window and interactive_window == wid:
        await asyncio.sleep(0.2)
        await handle_interactive_ui(context.bot, user.id, wid, thread_id)


# --- Window creation helper ---


async def _create_and_bind_window(
    query: object,
    context: ContextTypes.DEFAULT_TYPE,
    user: object,
    selected_path: str,
    pending_thread_id: int | None,
    resume_session_id: str | None = None,
    runtime_name: str = "codex",
) -> None:
    """Create a tmux window, bind it to a topic, and forward pending text.

    Shared by CB_DIR_CONFIRM (no sessions), CB_SESSION_NEW, and CB_SESSION_SELECT.
    """
    from telegram import CallbackQuery, User

    assert isinstance(query, CallbackQuery)
    assert isinstance(user, User)

    runtime = get_runtime(runtime_name)
    success, message, created_wname, created_wid = await tmux_manager.create_window(
        selected_path, resume_session_id=resume_session_id, runtime=runtime
    )
    if success:
        logger.info(
            "Window created: %s (id=%s) at %s (user=%d, thread=%s, resume=%s, runtime=%s)",
            created_wname,
            created_wid,
            selected_path,
            user.id,
            pending_thread_id,
            resume_session_id,
            runtime.name,
        )

        # Record runtime + cwd up front so the monitor and any future
        # detection paths know which agent is running here.
        ws = session_manager.get_window_state(created_wid)
        ws.runtime = runtime.name
        ws.cwd = selected_path
        ws.window_name = created_wname
        session_manager._save_state()

        if runtime.name == "claude":
            pane_pid = await tmux_manager.get_pane_pid(created_wid)
            sid = await runtime.discover_session_id(
                window_id=created_wid,
                pane_pid=pane_pid,
                cwd=selected_path,
                allow_cwd_fallback=True,
            )
            if sid:
                ws.session_id = sid
                session_manager._save_state()
                logger.info(
                    "claude session detected for window=%s sid=%s pane_pid=%s",
                    created_wid,
                    sid,
                    pane_pid,
                )
            else:
                logger.warning(
                    "claude session not detected for window=%s pane_pid=%s; "
                    "keystrokes will still be forwarded",
                    created_wid,
                    pane_pid,
                )
            # Fall through to thread bind + forward pending text.
            session_detected = bool(sid)
        else:
            # Detect session id for the new window.
            # Resume sessions can take longer while state is restored.
            if not resume_session_id:
                session_manager.mark_window_for_new_session(
                    created_wid,
                    clear_existing=False,
                )
            detect_timeout = 15.0 if resume_session_id else 5.0
            session_detected = await session_manager.wait_for_session_map_entry(
                created_wid, timeout=detect_timeout
            )

        # `resume` can create a new runtime session_id while messages continue
        # writing to the resumed session's JSONL file. Override window_state to
        # track the resumed session_id so the monitor can route messages back.
        if resume_session_id:
            ws = session_manager.get_window_state(created_wid)
            if not session_detected:
                # Detection timed out — manually populate window_state so the
                # monitor can still route messages back to this topic.
                logger.warning(
                    "Session detection timed out for resume window %s, "
                    "manually setting session_id=%s cwd=%s",
                    created_wid,
                    resume_session_id,
                    selected_path,
                )
                ws.session_id = resume_session_id
                ws.cwd = str(selected_path)
                ws.window_name = created_wname
                session_manager._save_state()
            elif ws.session_id != resume_session_id:
                logger.info(
                    "Resume override: window %s session_id %s -> %s",
                    created_wid,
                    ws.session_id,
                    resume_session_id,
                )
                ws.session_id = resume_session_id
                session_manager._save_state()

        # Prime the monitor's initial byte offset to EOF for the just-created
        # session so it doesn't tail-replay the existing transcript. Critical
        # for `resume` (the JSONL is already huge) and a no-op for fresh
        # sessions that haven't started writing yet.
        await _prime_attached_session_tracking(created_wid)

        if pending_thread_id is not None:
            # Thread bind flow: bind thread to newly created window
            session_manager.bind_thread(
                user.id, pending_thread_id, created_wid, window_name=created_wname
            )

            # Rename the topic to match the window name
            resolved_chat = session_manager.resolve_chat_id(user.id, pending_thread_id)
            try:
                await context.bot.edit_forum_topic(
                    chat_id=resolved_chat,
                    message_thread_id=pending_thread_id,
                    name=created_wname,
                )
            except Exception as e:
                logger.debug(f"Failed to rename topic: {e}")

            status = "Resumed" if resume_session_id else "Created"
            await safe_edit(
                query,
                f"✅ {message}\n\n{status}. Send messages here.",
            )

            # Send pending text if any
            pending_text = (
                context.user_data.get("_pending_thread_text")
                if context.user_data
                else None
            )
            if pending_text is not None:
                logger.debug(
                    "Forwarding pending text to window %s (len=%d)",
                    created_wname,
                    len(pending_text),
                )
                armed_skill = _get_armed_skill(context.user_data, pending_thread_id)
                text_to_send, unknown_single_token = _normalize_text_for_dispatch(
                    pending_text,
                    armed_skill,
                    runtime=runtime.name,
                )
                if unknown_single_token is not None:
                    _log_dispatch_event(
                        event="dispatch_reject_unknown_single_token",
                        user_id=user.id,
                        thread_id=pending_thread_id,
                        window_id=created_wid,
                        message_len=len(unknown_single_token),
                        is_complete=False,
                        skill_token=unknown_single_token,
                        reason="unknown_single_token",
                    )
                    logger.warning(
                        "Rejecting unarmed single-token pending text for thread=%s user=%s: %s",
                        pending_thread_id,
                        user.id,
                        unknown_single_token,
                    )
                    await safe_send(
                        context.bot,
                        resolved_chat,
                        _unknown_single_token_message(unknown_single_token),
                        message_thread_id=pending_thread_id,
                    )
                    if context.user_data is not None:
                        context.user_data.pop("_pending_thread_text", None)
                        context.user_data.pop("_pending_thread_id", None)
                else:
                    assert text_to_send is not None
                    send_ok, send_msg = await session_manager.send_to_window(
                        created_wid,
                        text_to_send,
                    )
                    if not send_ok:
                        _log_dispatch_event(
                            event="send_to_window_failed",
                            user_id=user.id,
                            thread_id=pending_thread_id,
                            window_id=created_wid,
                            message_len=len(text_to_send or ""),
                            is_complete=False,
                            reason=send_msg,
                            skill_token=text_to_send if armed_skill else None,
                        )
                        logger.warning("Failed to forward pending text: %s", send_msg)
                        await safe_send(
                            context.bot,
                            resolved_chat,
                            f"❌ Failed to send pending message: {send_msg}",
                            message_thread_id=pending_thread_id,
                        )
                    else:
                        if context.user_data is not None:
                            context.user_data.pop("_pending_thread_text", None)
                            context.user_data.pop("_pending_thread_id", None)
                        if armed_skill:
                            _clear_armed_skill(context.user_data, pending_thread_id)
            elif context.user_data is not None:
                context.user_data.pop("_pending_thread_id", None)
        else:
            # Should not happen in topic-only mode, but handle gracefully
            await safe_edit(query, f"✅ {message}")
    else:
        await safe_edit(query, f"❌ {message}")
        if pending_thread_id is not None and context.user_data is not None:
            context.user_data.pop("_pending_thread_id", None)
            context.user_data.pop("_pending_thread_text", None)
    await _safe_answer_callback_query(query, "Created" if success else "Failed")


# --- Callback query handler ---


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return

    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        await _safe_answer_callback_query(query, "Not authorized")
        return

    data = query.data
    await _safe_answer_callback_query(query)

    # Capture group chat_id for supergroup forum topic routing.
    # Required: Telegram Bot API needs group chat_id (not user_id) to send
    # messages with message_thread_id. Do NOT remove — see session.py docs.
    cb_thread_id = _get_thread_id(update)
    chat = update.effective_chat
    if chat and cb_thread_id is not None:
        session_manager.set_group_chat_id(user.id, cb_thread_id, chat.id)

    # History: older/newer pagination
    # Format: hp:<page>:<window_id>:<start>:<end> or hn:<page>:<window_id>:<start>:<end>
    if data.startswith(CB_HISTORY_PREV) or data.startswith(CB_HISTORY_NEXT):
        prefix_len = len(CB_HISTORY_PREV)  # same length for both
        rest = data[prefix_len:]
        try:
            parts = rest.split(":")
            if len(parts) < 4:
                # Old format without byte range: page:window_id
                offset_str, window_id = rest.split(":", 1)
                start_byte, end_byte = 0, 0
            else:
                # New format: page:window_id:start:end (window_id may contain colons)
                offset_str = parts[0]
                start_byte = int(parts[-2])
                end_byte = int(parts[-1])
                window_id = ":".join(parts[1:-2])
            offset = int(offset_str)
        except (ValueError, IndexError):
            await _safe_answer_callback_query(query, "Invalid data")
            return

        w = await tmux_manager.find_window_by_id(window_id)
        if w:
            await send_history(
                query,
                window_id,
                offset=offset,
                edit=True,
                start_byte=start_byte,
                end_byte=end_byte,
                # Don't pass user_id for pagination - offset update only on initial view
                # This prevents offset from going backwards if new messages arrive while paging
            )
        else:
            await safe_edit(query, "Window no longer exists.")
        await _safe_answer_callback_query(query, "Page updated")

    # Skills board: select skill, change page, or clear armed skill
    elif data.startswith(CB_SKILL_SELECT):
        current_tid = _get_thread_id(update)
        skill_tid = (
            context.user_data.get(SKILL_THREAD_KEY) if context.user_data else None
        )
        if skill_tid is not None and current_tid != skill_tid:
            await _safe_answer_callback_query(
                query, "Stale skills board (topic mismatch)", show_alert=True
            )
            return
        if current_tid is None:
            await _safe_answer_callback_query(
                query, "Use this in a named topic", show_alert=True
            )
            return
        try:
            idx = int(data[len(CB_SKILL_SELECT) :])
        except ValueError:
            await _safe_answer_callback_query(query, "Invalid data")
            return

        skills = context.user_data.get(SKILL_LIST_KEY, []) if context.user_data else []
        if idx < 0 or idx >= len(skills):
            await _safe_answer_callback_query(
                query,
                "Skill list changed, run /skillhelp again",
                show_alert=True,
            )
            return

        selected_skill = skills[idx]
        if context.user_data is not None:
            armed = _get_armed_skills(context.user_data)
            armed[current_tid] = selected_skill
            context.user_data[SKILL_PAGE_KEY] = idx // SKILLS_PER_PAGE
            context.user_data[SKILL_THREAD_KEY] = current_tid

        text, keyboard, page = _build_skill_board(
            skills,
            page=idx // SKILLS_PER_PAGE,
            armed_skill=selected_skill,
        )
        if context.user_data is not None:
            context.user_data[SKILL_PAGE_KEY] = page
        await safe_edit(query, text, reply_markup=keyboard)
        await _safe_answer_callback_query(query, f"Armed: {selected_skill}")

    elif data.startswith(CB_SKILL_PAGE):
        current_tid = _get_thread_id(update)
        skill_tid = (
            context.user_data.get(SKILL_THREAD_KEY) if context.user_data else None
        )
        if skill_tid is not None and current_tid != skill_tid:
            await _safe_answer_callback_query(
                query, "Stale skills board (topic mismatch)", show_alert=True
            )
            return

        skills = context.user_data.get(SKILL_LIST_KEY, []) if context.user_data else []
        if not skills:
            await _safe_answer_callback_query(
                query,
                "Skill list changed, run /skillhelp again",
                show_alert=True,
            )
            return

        try:
            requested_page = int(data[len(CB_SKILL_PAGE) :])
        except ValueError:
            await _safe_answer_callback_query(query, "Invalid data")
            return

        armed_skill = _get_armed_skill(context.user_data, current_tid)
        text, keyboard, page = _build_skill_board(
            skills,
            page=requested_page,
            armed_skill=armed_skill,
        )
        if context.user_data is not None:
            context.user_data[SKILL_PAGE_KEY] = page
        await safe_edit(query, text, reply_markup=keyboard)
        await _safe_answer_callback_query(
            query,
        )

    elif data == CB_SKILL_CANCEL:
        current_tid = _get_thread_id(update)
        skill_tid = (
            context.user_data.get(SKILL_THREAD_KEY) if context.user_data else None
        )
        if skill_tid is not None and current_tid != skill_tid:
            await _safe_answer_callback_query(
                query, "Stale skills board (topic mismatch)", show_alert=True
            )
            return

        _clear_armed_skill(context.user_data, current_tid)
        skills = context.user_data.get(SKILL_LIST_KEY, []) if context.user_data else []
        if not skills:
            await safe_edit(query, "Cancelled")
            await _safe_answer_callback_query(query, "Cancelled")
            return

        page = context.user_data.get(SKILL_PAGE_KEY, 0) if context.user_data else 0
        text, keyboard, page = _build_skill_board(skills, page=page, armed_skill=None)
        if context.user_data is not None:
            context.user_data[SKILL_PAGE_KEY] = page
        await safe_edit(query, text, reply_markup=keyboard)
        await _safe_answer_callback_query(query, "Cancelled")

    # Directory browser handlers
    elif data.startswith(CB_DIR_SELECT):
        # Validate: callback must come from the same topic that started browsing
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await _safe_answer_callback_query(
                query, "Stale browser (topic mismatch)", show_alert=True
            )
            return
        # callback_data contains index, not dir name (to avoid 64-byte limit)
        try:
            idx = int(data[len(CB_DIR_SELECT) :])
        except ValueError:
            await _safe_answer_callback_query(query, "Invalid data")
            return

        # Look up dir name from cached subdirs
        cached_dirs: list[str] = (
            context.user_data.get(BROWSE_DIRS_KEY, []) if context.user_data else []
        )
        if idx < 0 or idx >= len(cached_dirs):
            await _safe_answer_callback_query(
                query, "Directory list changed, please refresh", show_alert=True
            )
            return
        subdir_name = cached_dirs[idx]

        default_path = str(Path.cwd())
        current_path = (
            context.user_data.get(BROWSE_PATH_KEY, default_path)
            if context.user_data
            else default_path
        )
        new_path = (Path(current_path) / subdir_name).resolve()

        if not new_path.exists() or not new_path.is_dir():
            await _safe_answer_callback_query(
                query, "Directory not found", show_alert=True
            )
            return

        new_path_str = str(new_path)
        if context.user_data is not None:
            context.user_data[BROWSE_PATH_KEY] = new_path_str
            context.user_data[BROWSE_PAGE_KEY] = 0

        msg_text, keyboard, subdirs = build_directory_browser(new_path_str)
        if context.user_data is not None:
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await _safe_answer_callback_query(
            query,
        )

    elif data == CB_DIR_UP:
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await _safe_answer_callback_query(
                query, "Stale browser (topic mismatch)", show_alert=True
            )
            return
        default_path = str(Path.cwd())
        current_path = (
            context.user_data.get(BROWSE_PATH_KEY, default_path)
            if context.user_data
            else default_path
        )
        current = Path(current_path).resolve()
        parent = current.parent
        # No restriction - allow navigating anywhere

        parent_path = str(parent)
        if context.user_data is not None:
            context.user_data[BROWSE_PATH_KEY] = parent_path
            context.user_data[BROWSE_PAGE_KEY] = 0

        msg_text, keyboard, subdirs = build_directory_browser(parent_path)
        if context.user_data is not None:
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await _safe_answer_callback_query(
            query,
        )

    elif data.startswith(CB_DIR_PAGE):
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await _safe_answer_callback_query(
                query, "Stale browser (topic mismatch)", show_alert=True
            )
            return
        try:
            pg = int(data[len(CB_DIR_PAGE) :])
        except ValueError:
            await _safe_answer_callback_query(query, "Invalid data")
            return
        default_path = str(Path.cwd())
        current_path = (
            context.user_data.get(BROWSE_PATH_KEY, default_path)
            if context.user_data
            else default_path
        )
        if context.user_data is not None:
            context.user_data[BROWSE_PAGE_KEY] = pg

        msg_text, keyboard, subdirs = build_directory_browser(current_path, pg)
        if context.user_data is not None:
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await _safe_answer_callback_query(
            query,
        )

    elif data == CB_DIR_CONFIRM:
        default_path = str(Path.cwd())
        selected_path = (
            context.user_data.get(BROWSE_PATH_KEY, default_path)
            if context.user_data
            else default_path
        )
        # Check if this was initiated from a thread bind flow
        pending_thread_id: int | None = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )

        # Validate: confirm button must come from the same topic that started browsing
        confirm_thread_id = _get_thread_id(update)
        if pending_thread_id is not None and confirm_thread_id != pending_thread_id:
            clear_browse_state(context.user_data)
            if context.user_data is not None:
                context.user_data.pop("_pending_thread_id", None)
                context.user_data.pop("_pending_thread_text", None)
            await _safe_answer_callback_query(
                query, "Stale browser (topic mismatch)", show_alert=True
            )
            return

        clear_browse_state(context.user_data)

        runtime_name = (
            context.user_data.get(PENDING_RUNTIME_KEY, "codex")
            if context.user_data
            else "codex"
        )

        # Claude Code session indexing is not yet implemented; always
        # create a fresh session for Claude in Phase 1.
        if runtime_name == "codex":
            sessions = await session_manager.list_sessions_for_directory(selected_path)
            if sessions:
                # Show session picker — store state for later
                if context.user_data is not None:
                    context.user_data[STATE_KEY] = STATE_SELECTING_SESSION
                    context.user_data[SESSIONS_KEY] = sessions
                    context.user_data["_selected_path"] = selected_path
                text, keyboard = build_session_picker(sessions)
                await safe_edit(query, text, reply_markup=keyboard)
                await _safe_answer_callback_query(
                    query,
                )
                return

        # No existing sessions (or Claude runtime) — create new window directly
        await _create_and_bind_window(
            query,
            context,
            user,
            selected_path,
            pending_thread_id,
            runtime_name=runtime_name,
        )

    elif data == CB_DIR_CANCEL:
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await _safe_answer_callback_query(
                query, "Stale browser (topic mismatch)", show_alert=True
            )
            return
        clear_browse_state(context.user_data)
        if context.user_data is not None:
            context.user_data.pop("_pending_thread_id", None)
            context.user_data.pop("_pending_thread_text", None)
            context.user_data.pop(PENDING_RUNTIME_KEY, None)
        await safe_edit(query, "Cancelled")
        await _safe_answer_callback_query(query, "Cancelled")

    # Session picker: resume existing session
    elif data.startswith(CB_SESSION_SELECT):
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        # Fallback: if _pending_thread_id was cleared (e.g. by a message in
        # another topic), recover it from the callback query's message context
        if pending_tid is None:
            pending_tid = _get_thread_id(update)
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await _safe_answer_callback_query(
                query, "Stale picker (topic mismatch)", show_alert=True
            )
            return
        try:
            idx = int(data[len(CB_SESSION_SELECT) :])
        except ValueError:
            await _safe_answer_callback_query(query, "Invalid data")
            return

        cached_sessions = (
            context.user_data.get(SESSIONS_KEY, []) if context.user_data else []
        )
        if idx < 0 or idx >= len(cached_sessions):
            await _safe_answer_callback_query(query, "Session not found")
            return

        session = cached_sessions[idx]
        selected_path = (
            context.user_data.get("_selected_path", str(Path.cwd()))
            if context.user_data
            else str(Path.cwd())
        )
        runtime_name = (
            context.user_data.get(PENDING_RUNTIME_KEY, "codex")
            if context.user_data
            else "codex"
        )
        clear_session_picker_state(context.user_data)
        if context.user_data is not None:
            context.user_data.pop("_selected_path", None)

        await _create_and_bind_window(
            query,
            context,
            user,
            selected_path,
            pending_tid,
            resume_session_id=session.session_id,
            runtime_name=runtime_name,
        )

    elif data == CB_SESSION_NEW:
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is None:
            pending_tid = _get_thread_id(update)
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await _safe_answer_callback_query(
                query, "Stale picker (topic mismatch)", show_alert=True
            )
            return
        selected_path = (
            context.user_data.get("_selected_path", str(Path.cwd()))
            if context.user_data
            else str(Path.cwd())
        )
        runtime_name = (
            context.user_data.get(PENDING_RUNTIME_KEY, "codex")
            if context.user_data
            else "codex"
        )
        clear_session_picker_state(context.user_data)
        if context.user_data is not None:
            context.user_data.pop("_selected_path", None)

        await _create_and_bind_window(
            query,
            context,
            user,
            selected_path,
            pending_tid,
            runtime_name=runtime_name,
        )

    elif data == CB_SESSION_CANCEL:
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await _safe_answer_callback_query(
                query, "Stale picker (topic mismatch)", show_alert=True
            )
            return
        clear_session_picker_state(context.user_data)
        if context.user_data is not None:
            context.user_data.pop("_pending_thread_id", None)
            context.user_data.pop("_pending_thread_text", None)
            context.user_data.pop("_selected_path", None)
            context.user_data.pop(PENDING_RUNTIME_KEY, None)
        await safe_edit(query, "Cancelled")
        await _safe_answer_callback_query(query, "Cancelled")

    # Window picker: bind existing window
    elif data.startswith(CB_WIN_BIND):
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await _safe_answer_callback_query(
                query, "Stale picker (topic mismatch)", show_alert=True
            )
            return
        try:
            idx = int(data[len(CB_WIN_BIND) :])
        except ValueError:
            await _safe_answer_callback_query(query, "Invalid data")
            return

        cached_windows: list[str] = (
            context.user_data.get(UNBOUND_WINDOWS_KEY, []) if context.user_data else []
        )
        if idx < 0 or idx >= len(cached_windows):
            await _safe_answer_callback_query(
                query, "Window list changed, please retry", show_alert=True
            )
            return
        selected_wid = cached_windows[idx]

        # Verify window still exists
        w = await tmux_manager.find_window_by_id(selected_wid)
        if not w:
            display = session_manager.get_display_name(selected_wid)
            await _safe_answer_callback_query(
                query, f"Window '{display}' no longer exists", show_alert=True
            )
            return

        thread_id = _get_thread_id(update)
        if thread_id is None:
            await _safe_answer_callback_query(query, "Not in a topic", show_alert=True)
            return

        display = w.window_name
        clear_window_picker_state(context.user_data)
        session_manager.bind_thread(
            user.id, thread_id, selected_wid, window_name=display
        )
        # Sniff the runtime from the pane's active command so future
        # routing (notifications/UI) goes to the right code path.
        ws = session_manager.get_window_state(selected_wid)
        if get_runtime("claude").pane_command_matches(w.pane_current_command):
            ws.runtime = "claude"
        elif get_runtime("codex").pane_command_matches(w.pane_current_command):
            ws.runtime = "codex"
        if w.cwd and not ws.cwd:
            ws.cwd = w.cwd
        if display and not ws.window_name:
            ws.window_name = display
        session_manager._save_state()
        await _prime_attached_session_tracking(selected_wid)

        # Rename the topic to match the window name
        resolved_chat = session_manager.resolve_chat_id(user.id, thread_id)
        try:
            await context.bot.edit_forum_topic(
                chat_id=resolved_chat,
                message_thread_id=thread_id,
                name=display,
            )
        except Exception as e:
            logger.debug(f"Failed to rename topic: {e}")

        await safe_edit(
            query,
            f"✅ Bound to window `{display}`",
        )

        # Forward pending text if any
        pending_text = (
            context.user_data.get("_pending_thread_text") if context.user_data else None
        )
        armed_skill = _get_armed_skill(context.user_data, thread_id)
        bound_runtime = session_manager.get_window_state(selected_wid).runtime
        if pending_text is not None:
            text_to_send, unknown_single_token = _normalize_text_for_dispatch(
                pending_text,
                armed_skill,
                runtime=bound_runtime,
            )
            if unknown_single_token is not None:
                _log_dispatch_event(
                    event="dispatch_reject_unknown_single_token",
                    user_id=user.id,
                    thread_id=thread_id,
                    window_id=selected_wid,
                    message_len=len(unknown_single_token),
                    is_complete=False,
                    skill_token=unknown_single_token,
                    reason="unknown_single_token",
                )
                logger.warning(
                    "Rejecting unarmed single-token pending text for thread=%s user=%s: %s",
                    thread_id,
                    user.id,
                    unknown_single_token,
                )
                await safe_send(
                    context.bot,
                    resolved_chat,
                    _unknown_single_token_message(unknown_single_token),
                    message_thread_id=thread_id,
                )
                if context.user_data is not None:
                    context.user_data.pop("_pending_thread_text", None)
                    context.user_data.pop("_pending_thread_id", None)
            else:
                assert text_to_send is not None
                send_ok, send_msg = await session_manager.send_to_window(
                    selected_wid, text_to_send
                )
                if not send_ok:
                    _log_dispatch_event(
                        event="send_to_window_failed",
                        user_id=user.id,
                        thread_id=thread_id,
                        window_id=selected_wid,
                        message_len=len(text_to_send),
                        is_complete=False,
                        reason=send_msg,
                        skill_token=text_to_send if armed_skill else None,
                    )
                    logger.warning("Failed to forward pending text: %s", send_msg)
                    await safe_send(
                        context.bot,
                        resolved_chat,
                        f"❌ Failed to send pending message: {send_msg}",
                        message_thread_id=thread_id,
                    )
                else:
                    if context.user_data is not None:
                        context.user_data.pop("_pending_thread_text", None)
                        context.user_data.pop("_pending_thread_id", None)
                    if armed_skill:
                        _clear_armed_skill(context.user_data, thread_id)
        elif context.user_data is not None:
            context.user_data.pop("_pending_thread_id", None)
        await _safe_answer_callback_query(query, "Bound")

    # Window picker: new session → transition to runtime picker
    elif data == CB_WIN_NEW:
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await _safe_answer_callback_query(
                query, "Stale picker (topic mismatch)", show_alert=True
            )
            return
        # Preserve pending thread info, clear only picker state
        clear_window_picker_state(context.user_data)
        msg_text, keyboard = build_runtime_picker()
        if context.user_data is not None:
            context.user_data[STATE_KEY] = STATE_SELECTING_RUNTIME
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await _safe_answer_callback_query(
            query,
        )

    # Runtime picker: chose Codex or Claude → transition to directory browser
    elif data in (CB_RUNTIME_CODEX, CB_RUNTIME_CLAUDE):
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await _safe_answer_callback_query(
                query, "Stale picker (topic mismatch)", show_alert=True
            )
            return
        runtime_name = "codex" if data == CB_RUNTIME_CODEX else "claude"
        clear_runtime_picker_state(context.user_data)
        start_path = str(Path.cwd())
        msg_text, keyboard, subdirs = build_directory_browser(start_path)
        if context.user_data is not None:
            context.user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
            context.user_data[BROWSE_PATH_KEY] = start_path
            context.user_data[BROWSE_PAGE_KEY] = 0
            context.user_data[BROWSE_DIRS_KEY] = subdirs
            context.user_data[PENDING_RUNTIME_KEY] = runtime_name
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await _safe_answer_callback_query(
            query,
        )

    # Runtime picker: cancel
    elif data == CB_RUNTIME_CANCEL:
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await _safe_answer_callback_query(
                query, "Stale picker (topic mismatch)", show_alert=True
            )
            return
        clear_runtime_picker_state(context.user_data)
        if context.user_data is not None:
            context.user_data.pop("_pending_thread_id", None)
            context.user_data.pop("_pending_thread_text", None)
            context.user_data.pop(PENDING_RUNTIME_KEY, None)
        await safe_edit(query, "Cancelled")
        await _safe_answer_callback_query(query, "Cancelled")

    # Window picker: cancel
    elif data == CB_WIN_CANCEL:
        pending_tid = (
            context.user_data.get("_pending_thread_id") if context.user_data else None
        )
        if pending_tid is not None and _get_thread_id(update) != pending_tid:
            await _safe_answer_callback_query(
                query, "Stale picker (topic mismatch)", show_alert=True
            )
            return
        clear_window_picker_state(context.user_data)
        if context.user_data is not None:
            context.user_data.pop("_pending_thread_id", None)
            context.user_data.pop("_pending_thread_text", None)
        await safe_edit(query, "Cancelled")
        await _safe_answer_callback_query(query, "Cancelled")

    # Screenshot: Refresh
    elif data.startswith(CB_SCREENSHOT_REFRESH):
        window_id = data[len(CB_SCREENSHOT_REFRESH) :]
        thread_id = _get_thread_id(update)
        if not await _guard_callback_window_match(
            query=query,
            user_id=user.id,
            thread_id=thread_id,
            window_id=window_id,
            user_data=context.user_data,
            reason="Stale screenshot callback (topic/window mismatch)",
        ):
            return
        await _drain_thread_queue(user.id, thread_id)
        w = await tmux_manager.find_window_by_id(window_id)
        if not w:
            await _safe_answer_callback_query(
                query, "Window no longer exists", show_alert=True
            )
            return

        text = await _capture_screenshot_text(w.window_id)
        if not text:
            await _safe_answer_callback_query(
                query, "Failed to capture pane", show_alert=True
            )
            return

        png_bytes = await text_to_image(
            text,
            font_size=SCREENSHOT_FONT_SIZE,
            with_ansi=True,
        )
        keyboard = _build_screenshot_keyboard(window_id)
        mode = await _edit_screenshot_with_fallback(
            query=query,
            user_data=context.user_data,
            thread_id=thread_id,
            window_id=window_id,
            png_bytes=png_bytes,
            keyboard=keyboard,
            user_id=user.id,
        )
        if mode is not None:
            await _safe_answer_callback_query(query, "Refreshed")
        else:
            await _safe_answer_callback_query(
                query, "Failed to refresh", show_alert=True
            )

    # Structured interactive prompt: option selection
    elif data.startswith(CB_PROMPT_SELECT):
        thread_id = _get_thread_id(update)
        prompt_state = _get_structured_prompt_state(user.id, thread_id)
        if not prompt_state:
            await _safe_answer_callback_query(
                query, "Prompt is no longer active", show_alert=True
            )
            return
        if not _is_supported_structured_prompt_state(prompt_state):
            await _clear_structured_prompt_state(user.id, thread_id, bot=context.bot)
            await _safe_answer_callback_query(
                query, "Prompt is no longer active", show_alert=True
            )
            return

        window_id = str(prompt_state.get("window_id", ""))
        if not window_id:
            await _clear_structured_prompt_state(user.id, thread_id, bot=context.bot)
            await _safe_answer_callback_query(
                query, "Prompt state is invalid", show_alert=True
            )
            return

        if not await _guard_callback_window_match(
            query=query,
            user_id=user.id,
            thread_id=thread_id,
            window_id=window_id,
            user_data=context.user_data,
            reason="Stale prompt callback (topic/window mismatch)",
        ):
            await _clear_structured_prompt_state(user.id, thread_id, bot=context.bot)
            return

        state_message_id = prompt_state.get("message_id")
        query_message_id = getattr(query.message, "message_id", None)
        if (
            isinstance(state_message_id, int)
            and isinstance(query_message_id, int)
            and state_message_id != query_message_id
        ):
            await _safe_answer_callback_query(
                query, "Stale prompt message", show_alert=True
            )
            return

        try:
            option_index = int(data[len(CB_PROMPT_SELECT) :])
        except ValueError:
            await _safe_answer_callback_query(
                query, "Invalid selection", show_alert=True
            )
            return

        options = prompt_state.get("options")
        if (
            not isinstance(options, list)
            or option_index < 0
            or option_index >= len(options)
        ):
            await _safe_answer_callback_query(
                query, "Selection is out of range", show_alert=True
            )
            return

        selection_number = str(option_index + 1)
        selection_label = options[option_index].get("label", "")
        await _drain_thread_queue(user.id, thread_id)
        send_ok, send_message = await session_manager.send_to_window(
            window_id,
            selection_number,
        )
        if not send_ok:
            await _safe_answer_callback_query(
                query, f"Failed to send selection: {send_message}", show_alert=True
            )
            return

        confirm_text = f"✅ Selected option {selection_number}"
        if selection_label:
            confirm_text = f"{confirm_text}: `{selection_label}`"
        await safe_edit(query, confirm_text)
        await _safe_answer_callback_query(query, "Selected")
        await _clear_structured_prompt_state(
            user.id,
            thread_id,
            delete_message=False,
        )

    # Structured interactive prompt: pagination
    elif data.startswith(CB_PROMPT_PAGE):
        thread_id = _get_thread_id(update)
        prompt_state = _get_structured_prompt_state(user.id, thread_id)
        if not prompt_state:
            await _safe_answer_callback_query(
                query, "Prompt is no longer active", show_alert=True
            )
            return
        if not _is_supported_structured_prompt_state(prompt_state):
            await _clear_structured_prompt_state(user.id, thread_id, bot=context.bot)
            await _safe_answer_callback_query(
                query, "Prompt is no longer active", show_alert=True
            )
            return

        try:
            requested_page = int(data[len(CB_PROMPT_PAGE) :])
        except ValueError:
            await _safe_answer_callback_query(query, "Invalid page", show_alert=True)
            return

        text, bounded_page, _total_pages, _start, _end = _build_structured_prompt_view(
            prompt_state,
            requested_page,
        )
        keyboard, bounded_page = _build_structured_prompt_keyboard(
            prompt_state,
            requested_page,
        )
        prompt_state["page"] = bounded_page
        await safe_edit(query, text, reply_markup=keyboard)
        await _safe_answer_callback_query(
            query,
        )

    # Structured interactive prompt: cancel
    elif data == CB_PROMPT_CANCEL:
        thread_id = _get_thread_id(update)
        prompt_state = _get_structured_prompt_state(user.id, thread_id)
        if not prompt_state:
            await _safe_answer_callback_query(
                query, "Prompt is no longer active", show_alert=True
            )
            return
        if not _is_supported_structured_prompt_state(prompt_state):
            await _clear_structured_prompt_state(user.id, thread_id, bot=context.bot)
            await _safe_answer_callback_query(
                query, "Prompt is no longer active", show_alert=True
            )
            return

        window_id = str(prompt_state.get("window_id", ""))
        if window_id:
            w = await tmux_manager.find_window_by_id(window_id)
            if w:
                await tmux_manager.send_keys(
                    w.window_id,
                    "Escape",
                    enter=False,
                    literal=False,
                )

        await safe_edit(query, "Cancelled")
        await _safe_answer_callback_query(query, "Cancelled")
        await _clear_structured_prompt_state(
            user.id,
            thread_id,
            delete_message=False,
        )

    elif data == "noop":
        await _safe_answer_callback_query(
            query,
        )

    # Interactive UI: Up arrow
    elif data.startswith(CB_ASK_UP):
        window_id = data[len(CB_ASK_UP) :]
        thread_id = _get_thread_id(update)
        if not await _guard_callback_window_match(
            query=query,
            user_id=user.id,
            thread_id=thread_id,
            window_id=window_id,
            user_data=context.user_data,
            reason="Stale key callback (topic/window mismatch)",
        ):
            return
        w = await tmux_manager.find_window_by_id(window_id)
        if w:
            await tmux_manager.send_keys(w.window_id, "Up", enter=False, literal=False)
            await asyncio.sleep(0.5)
            await handle_interactive_ui(context.bot, user.id, window_id, thread_id)
        await _safe_answer_callback_query(
            query,
        )

    # Interactive UI: Down arrow
    elif data.startswith(CB_ASK_DOWN):
        window_id = data[len(CB_ASK_DOWN) :]
        thread_id = _get_thread_id(update)
        if not await _guard_callback_window_match(
            query=query,
            user_id=user.id,
            thread_id=thread_id,
            window_id=window_id,
            user_data=context.user_data,
            reason="Stale key callback (topic/window mismatch)",
        ):
            return
        w = await tmux_manager.find_window_by_id(window_id)
        if w:
            await tmux_manager.send_keys(
                w.window_id, "Down", enter=False, literal=False
            )
            await asyncio.sleep(0.5)
            await handle_interactive_ui(context.bot, user.id, window_id, thread_id)
        await _safe_answer_callback_query(
            query,
        )

    # Interactive UI: Left arrow
    elif data.startswith(CB_ASK_LEFT):
        window_id = data[len(CB_ASK_LEFT) :]
        thread_id = _get_thread_id(update)
        if not await _guard_callback_window_match(
            query=query,
            user_id=user.id,
            thread_id=thread_id,
            window_id=window_id,
            user_data=context.user_data,
            reason="Stale key callback (topic/window mismatch)",
        ):
            return
        w = await tmux_manager.find_window_by_id(window_id)
        if w:
            await tmux_manager.send_keys(
                w.window_id, "Left", enter=False, literal=False
            )
            await asyncio.sleep(0.5)
            await handle_interactive_ui(context.bot, user.id, window_id, thread_id)
        await _safe_answer_callback_query(
            query,
        )

    # Interactive UI: Right arrow
    elif data.startswith(CB_ASK_RIGHT):
        window_id = data[len(CB_ASK_RIGHT) :]
        thread_id = _get_thread_id(update)
        if not await _guard_callback_window_match(
            query=query,
            user_id=user.id,
            thread_id=thread_id,
            window_id=window_id,
            user_data=context.user_data,
            reason="Stale key callback (topic/window mismatch)",
        ):
            return
        w = await tmux_manager.find_window_by_id(window_id)
        if w:
            await tmux_manager.send_keys(
                w.window_id, "Right", enter=False, literal=False
            )
            await asyncio.sleep(0.5)
            await handle_interactive_ui(context.bot, user.id, window_id, thread_id)
        await _safe_answer_callback_query(
            query,
        )

    # Interactive UI: Previous question
    elif data.startswith(CB_ASK_PGUP):
        window_id = data[len(CB_ASK_PGUP) :]
        thread_id = _get_thread_id(update)
        if not await _guard_callback_window_match(
            query=query,
            user_id=user.id,
            thread_id=thread_id,
            window_id=window_id,
            user_data=context.user_data,
            reason="Stale key callback (topic/window mismatch)",
        ):
            return
        w = await tmux_manager.find_window_by_id(window_id)
        if w:
            await tmux_manager.send_keys(
                w.window_id, "PageUp", enter=False, literal=False
            )
            await asyncio.sleep(0.5)
            await handle_interactive_ui(context.bot, user.id, window_id, thread_id)
        await _safe_answer_callback_query(query, "⇞ Prev Q")

    # Interactive UI: Next question
    elif data.startswith(CB_ASK_PGDN):
        window_id = data[len(CB_ASK_PGDN) :]
        thread_id = _get_thread_id(update)
        if not await _guard_callback_window_match(
            query=query,
            user_id=user.id,
            thread_id=thread_id,
            window_id=window_id,
            user_data=context.user_data,
            reason="Stale key callback (topic/window mismatch)",
        ):
            return
        w = await tmux_manager.find_window_by_id(window_id)
        if w:
            await tmux_manager.send_keys(
                w.window_id, "PageDown", enter=False, literal=False
            )
            await asyncio.sleep(0.5)
            await handle_interactive_ui(context.bot, user.id, window_id, thread_id)
        await _safe_answer_callback_query(query, "⇟ Next Q")

    # Interactive UI: Escape
    elif data.startswith(CB_ASK_ESC):
        window_id = data[len(CB_ASK_ESC) :]
        thread_id = _get_thread_id(update)
        if not await _guard_callback_window_match(
            query=query,
            user_id=user.id,
            thread_id=thread_id,
            window_id=window_id,
            user_data=context.user_data,
            reason="Stale key callback (topic/window mismatch)",
        ):
            return
        w = await tmux_manager.find_window_by_id(window_id)
        if w:
            await tmux_manager.send_keys(
                w.window_id, "Escape", enter=False, literal=False
            )
            await clear_interactive_msg(user.id, context.bot, thread_id)
        await _safe_answer_callback_query(query, "⎋ Esc")

    # Interactive UI: Enter
    elif data.startswith(CB_ASK_ENTER):
        window_id = data[len(CB_ASK_ENTER) :]
        thread_id = _get_thread_id(update)
        if not await _guard_callback_window_match(
            query=query,
            user_id=user.id,
            thread_id=thread_id,
            window_id=window_id,
            user_data=context.user_data,
            reason="Stale key callback (topic/window mismatch)",
        ):
            return
        w = await tmux_manager.find_window_by_id(window_id)
        if w:
            await tmux_manager.send_keys(
                w.window_id, "Enter", enter=False, literal=False
            )
            await asyncio.sleep(0.5)
            await handle_interactive_ui(context.bot, user.id, window_id, thread_id)
        await _safe_answer_callback_query(query, "⏎ Enter")

    # Interactive UI: Space
    elif data.startswith(CB_ASK_SPACE):
        window_id = data[len(CB_ASK_SPACE) :]
        thread_id = _get_thread_id(update)
        if not await _guard_callback_window_match(
            query=query,
            user_id=user.id,
            thread_id=thread_id,
            window_id=window_id,
            user_data=context.user_data,
            reason="Stale key callback (topic/window mismatch)",
        ):
            return
        w = await tmux_manager.find_window_by_id(window_id)
        if w:
            await tmux_manager.send_keys(
                w.window_id, "Space", enter=False, literal=False
            )
            await asyncio.sleep(0.5)
            await handle_interactive_ui(context.bot, user.id, window_id, thread_id)
        await _safe_answer_callback_query(query, "␣ Space")

    # Interactive UI: Tab
    elif data.startswith(CB_ASK_TAB):
        window_id = data[len(CB_ASK_TAB) :]
        thread_id = _get_thread_id(update)
        if not await _guard_callback_window_match(
            query=query,
            user_id=user.id,
            thread_id=thread_id,
            window_id=window_id,
            user_data=context.user_data,
            reason="Stale key callback (topic/window mismatch)",
        ):
            return
        w = await tmux_manager.find_window_by_id(window_id)
        if w:
            await tmux_manager.send_keys(w.window_id, "Tab", enter=False, literal=False)
            await asyncio.sleep(0.5)
            await handle_interactive_ui(context.bot, user.id, window_id, thread_id)
        await _safe_answer_callback_query(query, "⇥ Tab")

    # Interactive UI: refresh display
    elif data.startswith(CB_ASK_REFRESH):
        window_id = data[len(CB_ASK_REFRESH) :]
        thread_id = _get_thread_id(update)
        if not await _guard_callback_window_match(
            query=query,
            user_id=user.id,
            thread_id=thread_id,
            window_id=window_id,
            user_data=context.user_data,
            reason="Stale key callback (topic/window mismatch)",
        ):
            return
        await handle_interactive_ui(context.bot, user.id, window_id, thread_id)
        await _safe_answer_callback_query(query, "🔄")

    # Screenshot quick keys: send key to tmux window
    elif data.startswith(CB_KEYS_PREFIX):
        rest = data[len(CB_KEYS_PREFIX) :]
        colon_idx = rest.find(":")
        if colon_idx < 0:
            await _safe_answer_callback_query(query, "Invalid data")
            return
        key_id = rest[:colon_idx]
        window_id = rest[colon_idx + 1 :]
        thread_id = _get_thread_id(update)

        key_info = _KEYS_SEND_MAP.get(key_id)
        if not key_info:
            await _safe_answer_callback_query(query, "Unknown key")
            return

        if not await _guard_callback_window_match(
            query=query,
            user_id=user.id,
            thread_id=thread_id,
            window_id=window_id,
            user_data=context.user_data,
            reason="Stale key callback (topic/window mismatch)",
        ):
            return

        tmux_key, enter, literal = key_info
        w = await tmux_manager.find_window_by_id(window_id)
        if not w:
            await _safe_answer_callback_query(
                query, "Window not found", show_alert=True
            )
            return

        await _drain_thread_queue(user.id, thread_id)

        await tmux_manager.send_keys(w.window_id, tmux_key, enter, literal)

        # Refresh screenshot after key press
        await asyncio.sleep(0.5)
        text = await _capture_screenshot_text(w.window_id)
        refreshed = False
        if text:
            png_bytes = await text_to_image(
                text,
                font_size=SCREENSHOT_FONT_SIZE,
                with_ansi=True,
            )
            keyboard = _build_screenshot_keyboard(window_id)
            mode = await _edit_screenshot_with_fallback(
                query=query,
                user_data=context.user_data,
                thread_id=thread_id,
                window_id=window_id,
                png_bytes=png_bytes,
                keyboard=keyboard,
                user_id=user.id,
            )
            refreshed = mode is not None
        label = _KEY_LABELS.get(key_id, key_id)
        if text and not refreshed:
            label = f"{label} (refresh failed)"
        await _safe_answer_callback_query(query, label)


# --- Streaming response / notifications ---


async def handle_new_message(msg: NewMessage, bot: Bot) -> None:
    """Handle a new assistant message — enqueue for sequential processing.

    Messages are queued per-user to ensure status messages always appear last.
    Routes via thread_bindings to deliver to the correct topic.
    """
    status = "complete" if msg.is_complete else "streaming"
    logger.info(
        f"handle_new_message [{status}]: session={msg.session_id}, "
        f"text_len={len(msg.text)}"
    )

    # Find users whose thread-bound window matches this session
    active_users = await session_manager.find_users_for_session(msg.session_id)

    if not active_users:
        # Web-only session: a tmux window exists with this session_id but no
        # Telegram topic is bound. The WebSocket transport handles delivery;
        # this is the expected state, not a failure.
        is_web_only_session = any(
            ws.session_id == msg.session_id
            for ws in session_manager.window_states.values()
        )
        if is_web_only_session:
            return

        window_snapshot = {
            window_id: session_manager.get_window_state(window_id).session_id
            for _uid, _tid, window_id in session_manager.iter_thread_bindings()
        }
        _log_dispatch_event(
            event="completion_enqueue_failed",
            user_id=None,
            thread_id=None,
            session_id=msg.session_id,
            turn_id=msg.turn_id,
            is_complete=msg.is_complete,
            message_len=len(msg.text),
            reason=f"no_active_users_for_session windows={window_snapshot}",
        )
        logger.info(
            "No active users for session %s (bound_window_sessions=%s)",
            msg.session_id,
            window_snapshot,
        )
        return

    for user_id, wid, thread_id in active_users:
        runtime_name = session_manager.get_window_state(wid).runtime

        if msg.message_type == "completion":
            pending_prompt = _pop_pending_post_completion_interactive(
                user_id, thread_id
            )
            if pending_prompt:
                await _clear_structured_prompt_state(user_id, thread_id, bot=bot)
                await _drain_thread_queue(user_id, thread_id)
                await asyncio.sleep(0.3)

                pending_window_id = pending_prompt.get("window_id", wid)
                pending_msg = pending_prompt.get("message")
                handled = await handle_interactive_ui(
                    bot,
                    user_id,
                    pending_window_id,
                    thread_id,
                )
                if not handled and isinstance(pending_msg, NewMessage):
                    handled = await render_interactive_message(
                        bot,
                        user_id,
                        pending_window_id,
                        _build_interactive_tool_fallback_text(pending_msg),
                        ui_name="ExitPlanMode",
                        thread_id=thread_id,
                    )
                if handled:
                    session = await session_manager.resolve_session_for_window(wid)
                    if session and session.file_path:
                        try:
                            file_size = Path(session.file_path).stat().st_size
                            session_manager.update_user_window_offset(
                                user_id, wid, file_size
                            )
                        except OSError:
                            pass
                    continue

            if get_interactive_msg_id(user_id, thread_id):
                await clear_interactive_msg(user_id, bot, thread_id)
            await _clear_structured_prompt_state(user_id, thread_id, bot=bot)

            try:
                await enqueue_completion_message(
                    bot=bot,
                    user_id=user_id,
                    window_id=wid,
                    completion_text=_build_completion_text(msg),
                    session_id=msg.session_id,
                    turn_id=msg.turn_id,
                    thread_id=thread_id,
                )
            except Exception as e:
                _log_dispatch_event(
                    event="completion_enqueue_failed",
                    user_id=user_id,
                    thread_id=thread_id,
                    window_id=wid,
                    session_id=msg.session_id,
                    turn_id=msg.turn_id,
                    is_complete=msg.is_complete,
                    message_len=len(msg.text),
                    reason=str(e),
                    error=True,
                )
                logger.warning(
                    "Failed to enqueue completion for user=%d session=%s thread=%d: %s",
                    user_id,
                    msg.session_id,
                    thread_id,
                    e,
                )
                continue

            session = await session_manager.resolve_session_for_window(wid)
            if session and session.file_path:
                try:
                    file_size = Path(session.file_path).stat().st_size
                    session_manager.update_user_window_offset(user_id, wid, file_size)
                except OSError:
                    pass
            continue

        # Handle interactive tools specially - capture terminal and send UI
        if msg.content_type == "tool_use" and is_interactive_tool_name(msg.tool_name):
            if _should_defer_interactive_prompt_until_completion(msg):
                await _clear_structured_prompt_state(user_id, thread_id, bot=bot)
                _set_pending_post_completion_interactive(
                    user_id,
                    thread_id,
                    {
                        "window_id": wid,
                        "message": msg,
                    },
                )
                continue

            handled_structured = await _try_handle_structured_interactive_prompt(
                bot,
                user_id,
                wid,
                thread_id,
                msg,
            )
            if handled_structured:
                session = await session_manager.resolve_session_for_window(wid)
                if session and session.file_path:
                    try:
                        file_size = Path(session.file_path).stat().st_size
                        session_manager.update_user_window_offset(
                            user_id, wid, file_size
                        )
                    except OSError:
                        pass
                continue

            await _clear_structured_prompt_state(user_id, thread_id, bot=bot)
            # Mark interactive mode BEFORE sleeping so polling skips this window
            set_interactive_mode(user_id, wid, thread_id)
            # Flush pending messages (e.g. plan content) before sending interactive UI
            queue = get_message_queue(user_id, thread_id)
            if queue:
                await queue.join()
            # Wait briefly for Codex to render the question UI
            await asyncio.sleep(0.3)
            handled = await handle_interactive_ui(bot, user_id, wid, thread_id)
            if handled:
                # Update user's read offset
                session = await session_manager.resolve_session_for_window(wid)
                if session and session.file_path:
                    try:
                        file_size = Path(session.file_path).stat().st_size
                        session_manager.update_user_window_offset(
                            user_id, wid, file_size
                        )
                    except OSError:
                        pass
                continue  # Don't send the normal tool_use message

            handled_fallback = await render_interactive_message(
                bot,
                user_id,
                wid,
                _build_interactive_tool_fallback_text(msg),
                thread_id=thread_id,
            )
            if handled_fallback:
                logger.info(
                    "Interactive tool rendered via fallback message "
                    "(tool=%s user=%d thread=%s window=%s)",
                    msg.tool_name,
                    user_id,
                    thread_id,
                    wid,
                )
                session = await session_manager.resolve_session_for_window(wid)
                if session and session.file_path:
                    try:
                        file_size = Path(session.file_path).stat().st_size
                        session_manager.update_user_window_offset(
                            user_id, wid, file_size
                        )
                    except OSError:
                        pass
                continue

            # UI not rendered — clear the early-set mode
            logger.warning(
                "Interactive tool detected but terminal UI not rendered "
                "(tool=%s user=%d thread=%s window=%s)",
                msg.tool_name,
                user_id,
                thread_id,
                wid,
            )
            clear_interactive_mode(user_id, thread_id)

        # Any non-interactive message means the interaction is complete — delete the UI message
        if get_interactive_msg_id(user_id, thread_id):
            await clear_interactive_msg(user_id, bot, thread_id)
        await _clear_structured_prompt_state(user_id, thread_id, bot=bot)

        parts = build_response_parts(
            msg.text,
            msg.is_complete,
            msg.content_type,
            msg.role,
        )

        if msg.is_complete:
            if _should_skip_progress_message(runtime_name, msg.content_type):
                session = await session_manager.resolve_session_for_window(wid)
                if session and session.file_path:
                    try:
                        file_size = Path(session.file_path).stat().st_size
                        session_manager.update_user_window_offset(
                            user_id, wid, file_size
                        )
                    except OSError:
                        pass
                continue

            if msg.role == "assistant" and msg.content_type in PROGRESS_CONTENT_TYPES:
                await enqueue_status_update(
                    bot,
                    user_id,
                    wid,
                    _build_progress_text(
                        content_type=msg.content_type,
                        raw_text=msg.text,
                        parts=parts,
                        tool_name=msg.tool_name,
                    ),
                    thread_id=thread_id,
                )

                session = await session_manager.resolve_session_for_window(wid)
                if session and session.file_path:
                    try:
                        file_size = Path(session.file_path).stat().st_size
                        session_manager.update_user_window_offset(
                            user_id, wid, file_size
                        )
                    except OSError:
                        pass
                continue

            # Enqueue content message task
            # Note: tool_result editing is handled inside _process_content_task
            # to ensure sequential processing with tool_use message sending
            await enqueue_content_message(
                bot=bot,
                user_id=user_id,
                window_id=wid,
                parts=parts,
                tool_use_id=msg.tool_use_id,
                content_type=msg.content_type,
                text=msg.text,
                thread_id=thread_id,
                image_data=msg.image_data,
                convert_status_to_content=False,
            )

            # Update user's read offset to current file position
            # This marks these messages as "read" for this user
            session = await session_manager.resolve_session_for_window(wid)
            if session and session.file_path:
                try:
                    file_size = Path(session.file_path).stat().st_size
                    session_manager.update_user_window_offset(user_id, wid, file_size)
                except OSError:
                    pass


# --- App lifecycle ---


async def post_init(application: Application) -> None:
    global session_monitor, _status_poll_task

    await application.bot.delete_my_commands()

    bot_commands = [
        BotCommand("start", "Show welcome message"),
        BotCommand("history", "Message history for this topic"),
        BotCommand("skillhelp", "How to use Codex skills"),
        BotCommand("status", "Show local topic status"),
        BotCommand("screenshot", "Terminal screenshot with control keys"),
        BotCommand("esc", "Send Escape to interrupt Codex"),
        BotCommand("diag", "Show topic completion diagnostics"),
        BotCommand("kill", "Kill session and delete topic"),
        BotCommand("unbind", "Unbind topic from session (keeps window running)"),
    ]
    # Add Codex slash commands
    for cmd_name, desc in CODEX_COMMANDS.items():
        bot_commands.append(BotCommand(cmd_name, desc))

    await application.bot.set_my_commands(bot_commands)

    # Re-resolve stale window IDs from persisted state against live tmux windows
    await session_manager.resolve_stale_ids()

    # Pre-fill global rate limiter bucket on restart.
    # AsyncLimiter starts at _level=0 (full burst capacity), but Telegram's
    # server-side counter persists across bot restarts.  Setting _level=max_rate
    # forces the bucket to start "full" so capacity drains in naturally (~1s).
    # AIORateLimiter has no per-private-chat limiter, so max_retries is the
    # primary protection (retry + pause all concurrent requests on 429).
    rate_limiter = application.bot.rate_limiter
    if rate_limiter and rate_limiter._base_limiter:
        rate_limiter._base_limiter._level = rate_limiter._base_limiter.max_rate
        logger.info("Pre-filled global rate limiter bucket")

    monitor = SessionMonitor()

    async def message_callback(msg: NewMessage) -> None:
        await handle_new_message(msg, application.bot)

    monitor.set_message_callback(message_callback)
    monitor.start()
    session_monitor = monitor
    logger.info("Session monitor started")

    # Start status polling task
    _status_poll_task = asyncio.create_task(status_poll_loop(application.bot))
    logger.info("Status polling task started")

    # Boot the web UI transport (no-op when WEB_UI_PASSWORD is unset)
    from .web import start_web_server

    await start_web_server(monitor, application.bot)


async def post_shutdown(application: Application) -> None:
    global _status_poll_task

    # Stop the web UI server first so in-flight requests can drain
    from .web import stop_web_server

    await stop_web_server(session_monitor)

    # Stop status polling
    if _status_poll_task:
        _status_poll_task.cancel()
        try:
            await _status_poll_task
        except asyncio.CancelledError:
            pass
        _status_poll_task = None
        logger.info("Status polling stopped")

    # Stop all queue workers
    await shutdown_workers()

    if session_monitor:
        await session_monitor.stop()
        logger.info("Session monitor stopped")

    await close_transcribe_client()


def create_bot() -> Application:
    application = (
        Application.builder()
        .token(config.telegram_bot_token)
        .rate_limiter(AIORateLimiter(max_retries=5))
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("skillhelp", skillhelp_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("diag", diag_command))
    application.add_handler(CommandHandler("screenshot", screenshot_command))
    application.add_handler(CommandHandler("esc", esc_command))
    application.add_handler(CommandHandler("kill", kill_command))
    application.add_handler(CommandHandler("unbind", unbind_command))
    application.add_handler(CallbackQueryHandler(callback_handler))
    # Topic closed event — auto-kill associated window
    application.add_handler(
        MessageHandler(
            filters.StatusUpdate.FORUM_TOPIC_CLOSED,
            topic_closed_handler,
        )
    )
    # Topic edited event — sync renamed topic to tmux window
    application.add_handler(
        MessageHandler(
            filters.StatusUpdate.FORUM_TOPIC_EDITED,
            topic_edited_handler,
        )
    )
    # Forward any other /command to Codex
    application.add_handler(MessageHandler(filters.COMMAND, forward_command_handler))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler)
    )
    # Photos: download and forward file path to Codex
    application.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    # Voice: transcribe via OpenAI and forward text to Codex
    application.add_handler(MessageHandler(filters.VOICE, voice_handler))
    # Catch-all: non-text content (stickers, video, etc.)
    application.add_handler(
        MessageHandler(
            ~filters.COMMAND & ~filters.TEXT & ~filters.StatusUpdate.ALL,
            unsupported_content_handler,
        )
    )

    return application
