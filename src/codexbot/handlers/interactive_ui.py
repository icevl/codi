"""Interactive UI handling for Codex prompts.

Handles interactive terminal UIs displayed by Codex:
  - AskUserQuestion: Multi-choice question prompts
  - ExitPlanMode: Plan mode exit confirmation
  - Permission Prompt: Tool permission requests
  - RestoreCheckpoint: Checkpoint restoration selection

Provides:
  - Keyboard navigation (up/down/left/right/enter/esc)
  - Terminal capture and display
  - Interactive mode tracking per user and thread

State dicts are keyed by (user_id, thread_id_or_0) for Telegram topic support.
"""

import json
import logging

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from ..config import config
from ..session import session_manager
from ..terminal_parser import extract_interactive_content, is_interactive_ui
from ..tmux_manager import tmux_manager
from ..utils import atomic_write_json
from .callback_data import (
    CB_ASK_DOWN,
    CB_ASK_ENTER,
    CB_ASK_ESC,
    CB_ASK_LEFT,
    CB_ASK_PGDN,
    CB_ASK_PGUP,
    CB_ASK_REFRESH,
    CB_ASK_RIGHT,
    CB_ASK_SPACE,
    CB_ASK_TAB,
    CB_ASK_UP,
)
from .message_sender import NO_LINK_PREVIEW

logger = logging.getLogger(__name__)

# Tool names that trigger interactive UI via JSONL (terminal capture + inline keyboard)
# Keep both legacy Codex names and modern API/function names.
INTERACTIVE_TOOL_NAMES = frozenset(
    {
        "AskUserQuestion",
        "request_user_input",
        "ExitPlanMode",
        "exit_plan_mode",
    }
)


def is_interactive_tool_name(tool_name: str | None) -> bool:
    """Return True when ``tool_name`` is one that renders interactive terminal UI."""
    if not tool_name:
        return False
    return tool_name in INTERACTIVE_TOOL_NAMES


# Track interactive UI message IDs: (user_id, thread_id_or_0) -> message_id.
# Persisted to disk so that, after a service restart, status polling can edit
# the existing Telegram message in place instead of re-sending a duplicate.
_INTERACTIVE_STATE_FILE = config.config_dir / "interactive_state.json"


def _load_interactive_msgs() -> dict[tuple[int, int], int]:
    if not _INTERACTIVE_STATE_FILE.exists():
        return {}
    try:
        raw = json.loads(_INTERACTIVE_STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        logger.warning("Failed to load interactive state: %s", exc)
        return {}
    result: dict[tuple[int, int], int] = {}
    for entry in raw.get("interactive_msgs", []):
        try:
            user_id = int(entry["user_id"])
            thread_id = int(entry.get("thread_id", 0))
            msg_id = int(entry["msg_id"])
        except (KeyError, TypeError, ValueError):
            continue
        result[(user_id, thread_id)] = msg_id
    return result


def _save_interactive_msgs() -> None:
    data = {
        "interactive_msgs": [
            {"user_id": uid, "thread_id": tid, "msg_id": mid}
            for (uid, tid), mid in _interactive_msgs.items()
        ],
    }
    try:
        atomic_write_json(_INTERACTIVE_STATE_FILE, data)
    except OSError as exc:
        logger.warning("Failed to save interactive state: %s", exc)


_interactive_msgs: dict[tuple[int, int], int] = _load_interactive_msgs()

# Track interactive mode: (user_id, thread_id_or_0) -> window_id.
# Re-derived from status polling within ~1s of restart; no need to persist.
_interactive_mode: dict[tuple[int, int], str] = {}


def get_interactive_window(user_id: int, thread_id: int | None = None) -> str | None:
    """Get the window_id for user's interactive mode."""
    return _interactive_mode.get((user_id, thread_id or 0))


def set_interactive_mode(
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
) -> None:
    """Set interactive mode for a user."""
    logger.debug(
        "Set interactive mode: user=%d, window_id=%s, thread=%s",
        user_id,
        window_id,
        thread_id,
    )
    _interactive_mode[(user_id, thread_id or 0)] = window_id


def clear_interactive_mode(user_id: int, thread_id: int | None = None) -> None:
    """Clear interactive mode for a user (without deleting message)."""
    logger.debug("Clear interactive mode: user=%d, thread=%s", user_id, thread_id)
    _interactive_mode.pop((user_id, thread_id or 0), None)


def get_interactive_msg_id(user_id: int, thread_id: int | None = None) -> int | None:
    """Get the interactive message ID for a user."""
    return _interactive_msgs.get((user_id, thread_id or 0))


def _build_interactive_keyboard(
    window_id: str,
    ui_name: str = "",
) -> InlineKeyboardMarkup:
    """Build keyboard for interactive UI navigation.

    ``ui_name`` controls the layout: ``RestoreCheckpoint`` omits ←/→ keys
    since only vertical selection is needed.
    """
    vertical_only = ui_name == "RestoreCheckpoint"
    question_navigation = ui_name == "AskUserQuestion"

    rows: list[list[InlineKeyboardButton]] = []
    # Row 1: directional keys
    rows.append(
        [
            InlineKeyboardButton(
                "␣ Space", callback_data=f"{CB_ASK_SPACE}{window_id}"[:64]
            ),
            InlineKeyboardButton("↑", callback_data=f"{CB_ASK_UP}{window_id}"[:64]),
            InlineKeyboardButton(
                "⇥ Tab", callback_data=f"{CB_ASK_TAB}{window_id}"[:64]
            ),
        ]
    )
    if vertical_only:
        rows.append(
            [
                InlineKeyboardButton(
                    "↓", callback_data=f"{CB_ASK_DOWN}{window_id}"[:64]
                ),
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    "←", callback_data=f"{CB_ASK_LEFT}{window_id}"[:64]
                ),
                InlineKeyboardButton(
                    "↓", callback_data=f"{CB_ASK_DOWN}{window_id}"[:64]
                ),
                InlineKeyboardButton(
                    "→", callback_data=f"{CB_ASK_RIGHT}{window_id}"[:64]
                ),
            ]
        )
    if question_navigation:
        rows.append(
            [
                InlineKeyboardButton(
                    "⇞ Prev Q", callback_data=f"{CB_ASK_PGUP}{window_id}"[:64]
                ),
                InlineKeyboardButton(
                    "⇟ Next Q", callback_data=f"{CB_ASK_PGDN}{window_id}"[:64]
                ),
            ]
        )
    # Row 2: action keys
    rows.append(
        [
            InlineKeyboardButton(
                "⎋ Esc", callback_data=f"{CB_ASK_ESC}{window_id}"[:64]
            ),
            InlineKeyboardButton(
                "🔄", callback_data=f"{CB_ASK_REFRESH}{window_id}"[:64]
            ),
            InlineKeyboardButton(
                "⏎ Enter", callback_data=f"{CB_ASK_ENTER}{window_id}"[:64]
            ),
        ]
    )
    return InlineKeyboardMarkup(rows)


async def render_interactive_message(
    bot: Bot,
    user_id: int,
    window_id: str,
    text: str,
    *,
    ui_name: str = "",
    thread_id: int | None = None,
) -> bool:
    """Send or update an interactive Telegram message with the standard key UI."""
    ikey = (user_id, thread_id or 0)
    chat_id = session_manager.resolve_chat_id(user_id, thread_id)
    keyboard = _build_interactive_keyboard(window_id, ui_name=ui_name)

    thread_kwargs: dict[str, int] = {}
    if thread_id is not None:
        thread_kwargs["message_thread_id"] = thread_id

    existing_msg_id = _interactive_msgs.get(ikey)
    if existing_msg_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=existing_msg_id,
                text=text,
                reply_markup=keyboard,
                link_preview_options=NO_LINK_PREVIEW,
            )
            _interactive_mode[ikey] = window_id
            return True
        except Exception:
            logger.debug(
                "Edit failed for interactive msg %s, sending new", existing_msg_id
            )
            _interactive_msgs.pop(ikey, None)
            _save_interactive_msgs()

    logger.info(
        "Sending interactive UI to user %d for window_id %s", user_id, window_id
    )
    try:
        sent = await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=keyboard,
            link_preview_options=NO_LINK_PREVIEW,
            **thread_kwargs,  # type: ignore[arg-type]
        )
    except Exception as e:
        logger.error("Failed to send interactive UI: %s", e)
        return False
    if sent:
        _interactive_msgs[ikey] = sent.message_id
        _interactive_mode[ikey] = window_id
        _save_interactive_msgs()
        return True
    return False


async def handle_interactive_ui(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
) -> bool:
    """Capture terminal and send interactive UI content to user.

    Handles AskUserQuestion, ExitPlanMode, Permission Prompt, and
    RestoreCheckpoint UIs. Returns True if UI was detected and sent,
    False otherwise.
    """
    w = await tmux_manager.find_window_by_id(window_id)
    if not w:
        return False

    # Capture plain text (no ANSI colors)
    pane_text = await tmux_manager.capture_pane(w.window_id)
    if not pane_text:
        logger.debug("No pane text captured for window_id %s", window_id)
        return False

    from ..session import session_manager

    runtime = session_manager.get_window_state(window_id).runtime

    # Quick check if it looks like an interactive UI
    if not is_interactive_ui(pane_text, runtime=runtime):
        logger.debug(
            "No interactive UI detected in window_id %s (last 3 lines: %s)",
            window_id,
            pane_text.strip().split("\n")[-3:],
        )
        return False

    # Extract content between separators
    content = extract_interactive_content(pane_text, runtime=runtime)
    if not content:
        return False

    return await render_interactive_message(
        bot,
        user_id,
        window_id,
        content.content,
        ui_name=content.name,
        thread_id=thread_id,
    )


async def clear_interactive_msg(
    user_id: int,
    bot: Bot | None = None,
    thread_id: int | None = None,
) -> None:
    """Clear tracked interactive message, delete from chat, and exit interactive mode."""
    ikey = (user_id, thread_id or 0)
    msg_id = _interactive_msgs.pop(ikey, None)
    _interactive_mode.pop(ikey, None)
    if msg_id is not None:
        _save_interactive_msgs()
    logger.debug(
        "Clear interactive msg: user=%d, thread=%s, msg_id=%s",
        user_id,
        thread_id,
        msg_id,
    )
    if bot and msg_id:
        chat_id = session_manager.resolve_chat_id(user_id, thread_id)
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass  # Message may already be deleted or too old
