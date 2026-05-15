"""Tests for Telegram skill-button flow."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import codexbot.bot as bot_mod
from telegram.error import BadRequest

from codexbot.bot import (
    ARMED_SKILLS_KEY,
    CB_WIN_BIND,
    SKILL_LIST_KEY,
    SKILL_THREAD_KEY,
    _prime_attached_session_tracking,
    _build_progress_text,
    _unknown_single_token_message,
    handle_new_message,
    _build_skill_board,
    callback_handler,
    diag_command,
    skillhelp_command,
    text_handler,
)
from codexbot.handlers.callback_data import (
    CB_PROMPT_CANCEL,
    CB_PROMPT_PAGE,
    CB_PROMPT_SELECT,
    CB_SESSION_NEW,
    CB_SESSION_SELECT,
    CB_SKILL_PAGE,
    CB_SKILL_SELECT,
)
from codexbot.handlers.directory_browser import UNBOUND_WINDOWS_KEY
from codexbot.handlers import message_queue as mq
from codexbot.session_monitor import NewMessage


@pytest.fixture(autouse=True)
def _clear_completion_turn_dedupe() -> None:
    mq._completion_pending_turns.clear()
    mq._completion_complete_turns.clear()
    mq._completion_diagnostic_events.clear()
    bot_mod._interactive_prompt_state.clear()
    bot_mod._pending_post_completion_interactive.clear()
    yield
    mq._completion_pending_turns.clear()
    mq._completion_complete_turns.clear()
    mq._completion_diagnostic_events.clear()
    bot_mod._interactive_prompt_state.clear()
    bot_mod._pending_post_completion_interactive.clear()


def _make_context() -> MagicMock:
    ctx = MagicMock()
    ctx.bot = AsyncMock()
    ctx.user_data = {}
    return ctx


def _make_text_update(text: str, thread_id: int = 42) -> MagicMock:
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = 1
    update.effective_chat = MagicMock()
    update.effective_chat.type = "private"
    update.effective_chat.id = 12345
    update.message = MagicMock()
    update.message.text = text
    update.message.message_thread_id = thread_id
    update.message.chat = MagicMock()
    update.message.chat.send_action = AsyncMock()
    return update


def _make_callback_update(data: str, thread_id: int = 42) -> MagicMock:
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = 1
    update.effective_chat = MagicMock()
    update.effective_chat.type = "private"
    update.effective_chat.id = 12345
    update.message = None
    update.callback_query = MagicMock()
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.message = MagicMock()
    update.callback_query.message.message_thread_id = thread_id
    return update


def test_build_skill_board_paginates_and_emits_callback_indices() -> None:
    skills = [f"skill-{i}" for i in range(10)]
    _text, keyboard, page = _build_skill_board(skills, page=1)
    assert page == 1

    cb_data = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data
    ]
    assert f"{CB_SKILL_SELECT}8" in cb_data
    assert f"{CB_SKILL_SELECT}9" in cb_data
    assert f"{CB_SKILL_PAGE}0" in cb_data


def test_build_progress_text_truncates_and_keeps_ellipsis() -> None:
    text = _build_progress_text(
        content_type="thinking",
        raw_text="x" * 5000,
        parts=[],
    )
    assert len(text) <= 3000
    assert text.endswith("…")


@pytest.mark.asyncio
async def test_skillhelp_command_shows_board_and_caches_skills() -> None:
    update = _make_text_update("/skillhelp", thread_id=42)
    context = _make_context()

    with (
        patch("codexbot.bot.is_user_allowed", return_value=True),
        patch("codexbot.bot._get_thread_id", return_value=42),
        patch("codexbot.bot.discover_skills", return_value=["alpha", "beta"]),
        patch("codexbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
    ):
        await skillhelp_command(update, context)

    assert context.user_data[SKILL_LIST_KEY] == ["alpha", "beta"]
    assert context.user_data[SKILL_THREAD_KEY] == 42
    assert mock_reply.await_count == 1
    assert mock_reply.await_args.kwargs.get("reply_markup") is not None


@pytest.mark.asyncio
async def test_diag_command_shows_recent_lifecycle_events() -> None:
    update = _make_text_update("/diag", thread_id=42)
    context = _make_context()
    context.user_data = {ARMED_SKILLS_KEY: {42: "gsd-quick"}}
    events = [
        {
            "timestamp": 1_700_000_100.0,
            "event": "finalized",
            "task_type": "completion",
            "window_id": "@7",
            "session_id": "s-1",
            "turn_id": 3,
            "queue_attempt": 0,
            "reason": "completion_complete",
        }
    ]

    with (
        patch("codexbot.bot.is_user_allowed", return_value=True),
        patch(
            "codexbot.bot.session_manager.resolve_window_for_thread", return_value="@7"
        ),
        patch(
            "codexbot.bot.session_manager.resolve_session_for_window",
            new_callable=AsyncMock,
            return_value=MagicMock(session_id="s-1"),
        ),
        patch("codexbot.bot.get_diagnostic_events", return_value=events),
        patch("codexbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
    ):
        await diag_command(update, context)

    assert mock_reply.await_count == 1
    text = mock_reply.await_args.args[1]
    assert "window=@7" in text
    assert "thread=42" in text
    assert "session=s-1" in text
    assert "recent:" in text
    assert "completion:finalized:completion_complete" in text
    assert context.user_data[ARMED_SKILLS_KEY][42] == "gsd-quick"


@pytest.mark.asyncio
async def test_diag_command_shows_empty_history_when_no_events() -> None:
    update = _make_text_update("/diag", thread_id=42)
    context = _make_context()
    with (
        patch("codexbot.bot.is_user_allowed", return_value=True),
        patch(
            "codexbot.bot.session_manager.resolve_window_for_thread", return_value="@8"
        ),
        patch(
            "codexbot.bot.session_manager.resolve_session_for_window",
            new_callable=AsyncMock,
            return_value=MagicMock(session_id="s-2"),
        ),
        patch("codexbot.bot.get_diagnostic_events", return_value=[]),
        patch("codexbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
    ):
        await diag_command(update, context)

    assert mock_reply.await_count == 1
    text = mock_reply.await_args.args[1]
    assert "window=@8" in text
    assert "No recent completion events." in text


@pytest.mark.asyncio
async def test_diag_command_reports_no_binding() -> None:
    update = _make_text_update("/diag", thread_id=42)
    context = _make_context()

    with (
        patch("codexbot.bot.is_user_allowed", return_value=True),
        patch(
            "codexbot.bot.session_manager.resolve_window_for_thread", return_value=None
        ),
        patch("codexbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
    ):
        await diag_command(update, context)

    assert mock_reply.await_count == 1
    assert (
        mock_reply.await_args.args[1]
        == "❌ No session bound to this topic. Send a message to start one."
    )


@pytest.mark.asyncio
async def test_skill_callback_select_arms_skill_for_thread() -> None:
    update = _make_callback_update(f"{CB_SKILL_SELECT}1", thread_id=42)
    context = _make_context()
    context.user_data = {
        SKILL_LIST_KEY: ["alpha", "beta", "gamma"],
        SKILL_THREAD_KEY: 42,
    }

    with (
        patch("codexbot.bot.is_user_allowed", return_value=True),
        patch("codexbot.bot.safe_edit", new_callable=AsyncMock) as mock_edit,
    ):
        await callback_handler(update, context)

    assert context.user_data[ARMED_SKILLS_KEY][42] == "beta"
    assert mock_edit.await_count == 1


@pytest.mark.asyncio
async def test_callback_handler_ignores_stale_query_answer_timeout() -> None:
    update = _make_callback_update(f"{CB_SKILL_PAGE}0", thread_id=42)
    context = _make_context()
    context.user_data = {
        SKILL_LIST_KEY: ["alpha", "beta"],
        SKILL_THREAD_KEY: 42,
    }
    update.callback_query.answer = AsyncMock(
        side_effect=[
            BadRequest("Query is too old and response timeout expired or query id is invalid"),
            BadRequest("Query is too old and response timeout expired or query id is invalid"),
        ]
    )

    with (
        patch("codexbot.bot.is_user_allowed", return_value=True),
        patch("codexbot.bot.safe_edit", new_callable=AsyncMock) as mock_edit,
    ):
        await callback_handler(update, context)

    mock_edit.assert_awaited_once()
    update.callback_query.answer.assert_awaited()


@pytest.mark.asyncio
async def test_text_handler_applies_armed_skill_and_clears_on_success() -> None:
    update = _make_text_update("do the thing", thread_id=42)
    context = _make_context()
    context.user_data = {ARMED_SKILLS_KEY: {42: "gsd-quick"}}

    with (
        patch("codexbot.bot.is_user_allowed", return_value=True),
        patch("codexbot.bot.session_manager") as mock_sm,
        patch("codexbot.bot.tmux_manager") as mock_tmux,
        patch("codexbot.bot.enqueue_status_update", new_callable=AsyncMock),
        patch("codexbot.bot.safe_reply", new_callable=AsyncMock),
    ):
        mock_sm.get_window_for_thread.return_value = "@5"
        mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock(window_id="@5"))
        mock_tmux.capture_pane = AsyncMock(return_value="")
        mock_sm.send_to_window = AsyncMock(return_value=(True, "ok"))

        await text_handler(update, context)

    mock_sm.send_to_window.assert_called_once_with("@5", "$gsd-quick do the thing")
    assert context.user_data[ARMED_SKILLS_KEY].get(42) is None


@pytest.mark.asyncio
async def test_text_handler_sends_single_known_skill_without_armed_skill() -> None:
    update = _make_text_update("$gsd-quick", thread_id=42)
    context = _make_context()

    with (
        patch("codexbot.bot.is_user_allowed", return_value=True),
        patch("codexbot.bot.session_manager") as mock_sm,
        patch("codexbot.bot.tmux_manager") as mock_tmux,
        patch(
            "codexbot.bot.enqueue_status_update", new_callable=AsyncMock
        ) as mock_enqueue,
        patch("codexbot.bot.logger"),
        patch("codexbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
    ):
        mock_sm.get_window_for_thread.return_value = "@5"
        mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock(window_id="@5"))
        mock_tmux.capture_pane = AsyncMock(return_value="")
        mock_sm.send_to_window = AsyncMock(return_value=(True, "ok"))

        await text_handler(update, context)

    mock_sm.send_to_window.assert_called_once_with("@5", "$gsd-quick")
    mock_enqueue.assert_awaited_once()
    update.message.chat.send_action.assert_not_called()
    mock_reply.assert_not_awaited()
    assert context.user_data.get(ARMED_SKILLS_KEY, {}) == {}


@pytest.mark.asyncio
async def test_text_handler_forwards_unknown_single_token_skill() -> None:
    update = _make_text_update("$foo", thread_id=42)
    context = _make_context()

    with (
        patch("codexbot.bot.is_user_allowed", return_value=True),
        patch("codexbot.bot.session_manager") as mock_sm,
        patch("codexbot.bot.tmux_manager") as mock_tmux,
        patch(
            "codexbot.bot.enqueue_status_update", new_callable=AsyncMock
        ) as mock_enqueue,
        patch("codexbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
    ):
        mock_sm.get_window_for_thread.return_value = "@5"
        mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock(window_id="@5"))
        mock_tmux.capture_pane = AsyncMock(return_value="")
        mock_sm.send_to_window = AsyncMock(return_value=(True, "ok"))

        await text_handler(update, context)

    mock_sm.send_to_window.assert_called_once_with("@5", "$foo")
    mock_enqueue.assert_awaited_once()
    update.message.chat.send_action.assert_not_called()
    mock_reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_text_handler_normalizes_skill_token_with_trailing_space() -> None:
    update = _make_text_update("$gsd-help   ", thread_id=42)
    context = _make_context()

    with (
        patch("codexbot.bot.is_user_allowed", return_value=True),
        patch("codexbot.bot.session_manager") as mock_sm,
        patch("codexbot.bot.tmux_manager") as mock_tmux,
        patch(
            "codexbot.bot.enqueue_status_update", new_callable=AsyncMock
        ) as mock_enqueue,
        patch("codexbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
    ):
        mock_sm.get_window_for_thread.return_value = "@5"
        mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock(window_id="@5"))
        mock_tmux.capture_pane = AsyncMock(return_value="")
        mock_sm.send_to_window = AsyncMock(return_value=(True, "ok"))

        await text_handler(update, context)

    mock_sm.send_to_window.assert_called_once_with("@5", "$gsd-help")
    mock_enqueue.assert_awaited_once()
    update.message.chat.send_action.assert_not_called()
    mock_reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_text_handler_rejects_malformed_bare_dollar_token() -> None:
    update = _make_text_update("$", thread_id=42)
    context = _make_context()

    with (
        patch("codexbot.bot.is_user_allowed", return_value=True),
        patch("codexbot.bot.session_manager") as mock_sm,
        patch("codexbot.bot.tmux_manager") as mock_tmux,
        patch(
            "codexbot.bot.enqueue_status_update", new_callable=AsyncMock
        ) as mock_enqueue,
        patch("codexbot.bot.logger") as mock_logger,
        patch("codexbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
    ):
        mock_sm.get_window_for_thread.return_value = "@5"
        mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock(window_id="@5"))
        mock_tmux.capture_pane = AsyncMock(return_value="")
        mock_sm.send_to_window = AsyncMock(return_value=(True, "ok"))

        await text_handler(update, context)

    mock_sm.send_to_window.assert_not_called()
    mock_enqueue.assert_not_awaited()
    update.message.chat.send_action.assert_not_called()
    mock_reply.assert_awaited_once()
    assert mock_reply.await_args.args[0] is update.message
    assert mock_reply.await_args.args[1] == _unknown_single_token_message("$")
    warning_call = mock_logger.warning.call_args.args[0]
    assert "dispatch_reject_unknown_single_token" in warning_call
    assert "user_id=1" in warning_call
    assert "thread_id=42" in warning_call
    assert "skill_token=$" in warning_call


@pytest.mark.asyncio
async def test_text_handler_forwards_plain_single_token_without_skill_prefix() -> None:
    update = _make_text_update("1", thread_id=42)
    context = _make_context()

    with (
        patch("codexbot.bot.is_user_allowed", return_value=True),
        patch(
            "codexbot.bot.discover_skills", return_value=["gsd-quick"]
        ) as mock_discover,
        patch("codexbot.bot.session_manager") as mock_sm,
        patch("codexbot.bot.tmux_manager") as mock_tmux,
        patch(
            "codexbot.bot.enqueue_status_update", new_callable=AsyncMock
        ) as mock_enqueue,
        patch("codexbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
    ):
        mock_sm.get_window_for_thread.return_value = "@5"
        mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock(window_id="@5"))
        mock_tmux.capture_pane = AsyncMock(return_value="")
        mock_sm.send_to_window = AsyncMock(return_value=(True, "ok"))

        await text_handler(update, context)

    mock_discover.assert_not_called()
    mock_sm.send_to_window.assert_called_once_with("@5", "1")
    mock_enqueue.assert_awaited_once()
    update.message.chat.send_action.assert_not_called()
    mock_reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_text_handler_keeps_unarmed_multi_word_text() -> None:
    update = _make_text_update("run gsd-quick please", thread_id=42)
    context = _make_context()

    with (
        patch("codexbot.bot.is_user_allowed", return_value=True),
        patch("codexbot.bot.discover_skills", return_value=["gsd-quick"]),
        patch("codexbot.bot.session_manager") as mock_sm,
        patch("codexbot.bot.tmux_manager") as mock_tmux,
        patch("codexbot.bot.enqueue_status_update", new_callable=AsyncMock),
        patch("codexbot.bot.safe_reply", new_callable=AsyncMock),
    ):
        mock_sm.get_window_for_thread.return_value = "@5"
        mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock(window_id="@5"))
        mock_tmux.capture_pane = AsyncMock(return_value="")
        mock_sm.send_to_window = AsyncMock(return_value=(True, "ok"))

        await text_handler(update, context)

    mock_sm.send_to_window.assert_called_once_with("@5", "run gsd-quick please")


@pytest.mark.asyncio
async def test_text_handler_keeps_armed_skill_on_send_failure() -> None:
    update = _make_text_update("do the thing", thread_id=42)
    context = _make_context()
    context.user_data = {ARMED_SKILLS_KEY: {42: "gsd-quick"}}

    with (
        patch("codexbot.bot.is_user_allowed", return_value=True),
        patch("codexbot.bot.session_manager") as mock_sm,
        patch("codexbot.bot.tmux_manager") as mock_tmux,
        patch("codexbot.bot.enqueue_status_update", new_callable=AsyncMock),
        patch("codexbot.bot.safe_reply", new_callable=AsyncMock),
    ):
        mock_sm.get_window_for_thread.return_value = "@5"
        mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock(window_id="@5"))
        mock_tmux.capture_pane = AsyncMock(return_value="")
        mock_sm.send_to_window = AsyncMock(return_value=(False, "failed"))

        await text_handler(update, context)

    mock_sm.send_to_window.assert_called_once_with("@5", "$gsd-quick do the thing")
    assert context.user_data[ARMED_SKILLS_KEY][42] == "gsd-quick"


@pytest.mark.asyncio
async def test_text_handler_sends_skill_for_empty_message() -> None:
    update = _make_text_update("", thread_id=42)
    context = _make_context()
    context.user_data = {ARMED_SKILLS_KEY: {42: "gsd-quick"}}

    with (
        patch("codexbot.bot.is_user_allowed", return_value=True),
        patch("codexbot.bot.session_manager") as mock_sm,
        patch("codexbot.bot.tmux_manager") as mock_tmux,
        patch("codexbot.bot.enqueue_status_update", new_callable=AsyncMock),
        patch("codexbot.bot.safe_reply", new_callable=AsyncMock),
    ):
        mock_sm.get_window_for_thread.return_value = "@5"
        mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock(window_id="@5"))
        mock_tmux.capture_pane = AsyncMock(return_value="")
        mock_sm.send_to_window = AsyncMock(return_value=(True, "ok"))

        await text_handler(update, context)

    mock_sm.send_to_window.assert_called_once_with("@5", "$gsd-quick")
    assert context.user_data[ARMED_SKILLS_KEY].get(42) is None


@pytest.mark.asyncio
async def test_text_handler_for_whitespace_with_armed_skill() -> None:
    update = _make_text_update("   \n\t", thread_id=42)
    context = _make_context()
    context.user_data = {ARMED_SKILLS_KEY: {42: "gsd-quick"}}

    with (
        patch("codexbot.bot.is_user_allowed", return_value=True),
        patch(
            "codexbot.bot.discover_skills", return_value=["gsd-quick"]
        ) as mock_discover,
        patch("codexbot.bot.session_manager") as mock_sm,
        patch("codexbot.bot.tmux_manager") as mock_tmux,
        patch(
            "codexbot.bot.enqueue_status_update", new_callable=AsyncMock
        ) as mock_enqueue,
        patch("codexbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
    ):
        mock_sm.get_window_for_thread.return_value = "@5"
        mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock(window_id="@5"))
        mock_tmux.capture_pane = AsyncMock(return_value="")
        mock_sm.send_to_window = AsyncMock(return_value=(True, "ok"))

        await text_handler(update, context)

    mock_discover.assert_not_called()
    mock_sm.send_to_window.assert_called_once_with("@5", "$gsd-quick")
    mock_enqueue.assert_awaited_once()
    update.message.chat.send_action.assert_not_called()
    mock_reply.assert_not_awaited()
    assert context.user_data[ARMED_SKILLS_KEY].get(42) is None


@pytest.mark.asyncio
async def test_callback_bind_forwards_empty_pending_text_with_armed_skill() -> None:
    update = _make_callback_update(f"{CB_WIN_BIND}0", thread_id=42)
    context = _make_context()
    context.user_data = {
        ARMED_SKILLS_KEY: {42: "gsd-quick"},
        UNBOUND_WINDOWS_KEY: ["@5"],
        "_pending_thread_id": 42,
        "_pending_thread_text": "",
    }

    with (
        patch("codexbot.bot.is_user_allowed", return_value=True),
        patch("codexbot.bot.session_manager") as mock_sm,
        patch("codexbot.bot._prime_attached_session_tracking", new_callable=AsyncMock),
        patch("codexbot.bot.tmux_manager") as mock_tmux,
        patch("codexbot.bot.safe_edit", new_callable=AsyncMock) as mock_edit,
        patch("codexbot.bot.safe_send", new_callable=AsyncMock),
    ):
        mock_tmux.find_window_by_id = AsyncMock(
            return_value=MagicMock(window_id="@5", window_name="window")
        )
        mock_sm.bind_thread = MagicMock()
        mock_sm.resolve_chat_id.return_value = 100
        mock_sm.send_to_window = AsyncMock(return_value=(True, "ok"))
        mock_sm.update_user_window_offset = MagicMock()

        await callback_handler(update, context)

    mock_sm.send_to_window.assert_called_once_with("@5", "$gsd-quick")
    assert context.user_data.get("_pending_thread_text") is None
    assert context.user_data.get("_pending_thread_id") is None
    mock_edit.assert_awaited()


@pytest.mark.asyncio
async def test_callback_bind_forwards_pending_text_with_send_failure_keeps_state() -> (
    None
):
    update = _make_callback_update(f"{CB_WIN_BIND}0", thread_id=42)
    context = _make_context()
    context.user_data = {
        ARMED_SKILLS_KEY: {42: "gsd-quick"},
        UNBOUND_WINDOWS_KEY: ["@5"],
        "_pending_thread_id": 42,
        "_pending_thread_text": "   \n\t",
    }

    with (
        patch("codexbot.bot.is_user_allowed", return_value=True),
        patch("codexbot.bot.session_manager") as mock_sm,
        patch("codexbot.bot._prime_attached_session_tracking", new_callable=AsyncMock),
        patch("codexbot.bot.tmux_manager") as mock_tmux,
        patch("codexbot.bot.safe_edit", new_callable=AsyncMock) as mock_edit,
        patch("codexbot.bot.safe_send", new_callable=AsyncMock) as mock_send,
    ):
        mock_tmux.find_window_by_id = AsyncMock(
            return_value=MagicMock(window_id="@5", window_name="window")
        )
        mock_sm.bind_thread = MagicMock()
        mock_sm.resolve_chat_id.return_value = 100
        mock_sm.send_to_window = AsyncMock(return_value=(False, "failed"))
        mock_sm.update_user_window_offset = MagicMock()

        await callback_handler(update, context)

    mock_sm.send_to_window.assert_called_once_with("@5", "$gsd-quick")
    assert context.user_data.get("_pending_thread_text") == "   \n\t"
    assert context.user_data.get("_pending_thread_id") == 42
    assert context.user_data[ARMED_SKILLS_KEY].get(42) == "gsd-quick"
    mock_edit.assert_awaited()
    mock_send.assert_awaited_once()


@pytest.mark.asyncio
async def test_prime_attached_session_tracking_uses_current_transcript_size(
    tmp_path,
) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"hello": "world"}\n', encoding="utf-8")
    monitor = MagicMock()

    with (
        patch("codexbot.bot.session_monitor", monitor),
        patch("codexbot.bot.session_manager") as mock_sm,
    ):
        mock_sm.resolve_session_for_window = AsyncMock(
            return_value=MagicMock(session_id="sid-1")
        )
        mock_sm.get_session_file_path = AsyncMock(return_value=transcript)

        await _prime_attached_session_tracking("@5")

    monitor.set_initial_offset.assert_called_once_with(
        "sid-1", transcript.stat().st_size
    )


@pytest.mark.asyncio
async def test_callback_bind_primes_tracking_before_forwarding_pending_text() -> None:
    update = _make_callback_update(f"{CB_WIN_BIND}0", thread_id=42)
    context = _make_context()
    context.user_data = {
        UNBOUND_WINDOWS_KEY: ["@5"],
        "_pending_thread_id": 42,
        "_pending_thread_text": "hello",
    }
    call_order: list[str] = []

    with (
        patch("codexbot.bot.is_user_allowed", return_value=True),
        patch("codexbot.bot.session_manager") as mock_sm,
        patch("codexbot.bot.tmux_manager") as mock_tmux,
        patch(
            "codexbot.bot._prime_attached_session_tracking", new_callable=AsyncMock
        ) as mock_prime,
        patch("codexbot.bot.safe_edit", new_callable=AsyncMock),
        patch("codexbot.bot.safe_send", new_callable=AsyncMock),
    ):
        mock_tmux.find_window_by_id = AsyncMock(
            return_value=MagicMock(window_id="@5", window_name="window")
        )
        mock_sm.bind_thread = MagicMock()
        mock_sm.resolve_chat_id.return_value = 100

        async def _prime(_wid: str) -> None:
            call_order.append("prime")

        async def _send(_wid: str, _text: str) -> tuple[bool, str]:
            call_order.append("send")
            return True, "ok"

        mock_prime.side_effect = _prime
        mock_sm.send_to_window = AsyncMock(side_effect=_send)

        await callback_handler(update, context)

    assert call_order == ["prime", "send"]


@pytest.mark.asyncio
async def test_text_handler_forwards_without_waiting_for_send_action() -> None:
    update = _make_text_update("hi", thread_id=42)
    update.message.chat.send_action.side_effect = RuntimeError("rate limited")
    context = _make_context()

    with (
        patch("codexbot.bot.is_user_allowed", return_value=True),
        patch("codexbot.bot.session_manager") as mock_sm,
        patch("codexbot.bot.tmux_manager") as mock_tmux,
        patch(
            "codexbot.bot.enqueue_status_update", new_callable=AsyncMock
        ) as mock_enqueue,
        patch("codexbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
    ):
        mock_sm.get_window_for_thread.return_value = "@5"
        mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock(window_id="@5"))
        mock_tmux.capture_pane = AsyncMock(return_value="")
        mock_sm.send_to_window = AsyncMock(return_value=(True, "ok"))

        await text_handler(update, context)

    mock_sm.send_to_window.assert_called_once_with("@5", "hi")
    mock_enqueue.assert_awaited_once()
    update.message.chat.send_action.assert_not_called()
    mock_reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_and_bind_window_forwards_pending_text_with_armed_skill() -> None:
    update = _make_callback_update(CB_SESSION_NEW, thread_id=42)
    context = _make_context()
    context.user_data = {
        ARMED_SKILLS_KEY: {42: "gsd-quick"},
        "_pending_thread_id": 42,
        "_pending_thread_text": "   \n\t",
        "_selected_path": "/tmp",
    }

    with (
        patch("codexbot.bot.is_user_allowed", return_value=True),
        patch("codexbot.bot.session_manager") as mock_sm,
        patch("codexbot.bot.tmux_manager") as mock_tmux,
        patch("codexbot.bot.safe_edit", new_callable=AsyncMock),
        patch("codexbot.bot.safe_send", new_callable=AsyncMock) as mock_send,
        patch("telegram.CallbackQuery", MagicMock),
        patch("telegram.User", MagicMock),
    ):
        mock_tmux.create_window = AsyncMock(
            return_value=(True, "Created", "window", "@7")
        )
        mock_sm.wait_for_session_map_entry = AsyncMock(return_value=True)
        mock_sm.bind_thread = MagicMock()
        mock_sm.resolve_chat_id.return_value = 100
        mock_sm.send_to_window = AsyncMock(return_value=(True, "ok"))
        mock_sm.update_user_window_offset = MagicMock()

        await callback_handler(update, context)

    mock_sm.send_to_window.assert_called_once_with("@7", "$gsd-quick")
    assert context.user_data.get("_pending_thread_text") is None
    assert context.user_data.get("_pending_thread_id") is None
    assert context.user_data[ARMED_SKILLS_KEY].get(42) is None
    mock_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_and_bind_window_send_failure_keeps_pending_and_armed_skill() -> (
    None
):
    update = _make_callback_update(CB_SESSION_NEW, thread_id=42)
    context = _make_context()
    context.user_data = {
        ARMED_SKILLS_KEY: {42: "gsd-quick"},
        "_pending_thread_id": 42,
        "_pending_thread_text": "   \n\t",
        "_selected_path": "/tmp",
    }

    with (
        patch("codexbot.bot.is_user_allowed", return_value=True),
        patch("codexbot.bot.session_manager") as mock_sm,
        patch("codexbot.bot.tmux_manager") as mock_tmux,
        patch("codexbot.bot.safe_edit", new_callable=AsyncMock),
        patch("codexbot.bot.safe_send", new_callable=AsyncMock) as mock_send,
        patch("telegram.CallbackQuery", MagicMock),
        patch("telegram.User", MagicMock),
    ):
        mock_tmux.create_window = AsyncMock(
            return_value=(True, "Created", "window", "@7")
        )
        mock_sm.wait_for_session_map_entry = AsyncMock(return_value=True)
        mock_sm.bind_thread = MagicMock()
        mock_sm.resolve_chat_id.return_value = 100
        mock_sm.send_to_window = AsyncMock(return_value=(False, "failed"))
        mock_sm.update_user_window_offset = MagicMock()

        await callback_handler(update, context)

    mock_sm.send_to_window.assert_called_once_with("@7", "$gsd-quick")
    assert context.user_data.get("_pending_thread_text") == "   \n\t"
    assert context.user_data.get("_pending_thread_id") == 42
    assert context.user_data[ARMED_SKILLS_KEY].get(42) == "gsd-quick"
    mock_send.assert_awaited_once()


@pytest.mark.asyncio
async def test_session_select_callback_stale_topic_is_rejected() -> None:
    update = _make_callback_update(f"{CB_SESSION_SELECT}0", thread_id=42)
    context = _make_context()
    context.user_data = {"_pending_thread_id": 7}

    with (
        patch("codexbot.bot.is_user_allowed", return_value=True),
    ):
        await callback_handler(update, context)

    update.callback_query.answer.assert_awaited_with(
        "Stale picker (topic mismatch)",
        show_alert=True,
    )


@pytest.mark.asyncio
async def test_handle_new_message_completion_creates_completion_message(
    tmp_path,
) -> None:
    mock_bot = AsyncMock()

    completion_msg = NewMessage(
        session_id="s1",
        text="",
        is_complete=True,
        message_type="completion",
        turn_id=7,
    )

    with (
        patch("codexbot.bot.session_manager") as mock_sm,
        patch(
            "codexbot.bot.enqueue_completion_message", new_callable=AsyncMock
        ) as mock_enqueue,
        patch("codexbot.bot.get_interactive_msg_id", return_value=None),
    ):
        mock_sm.find_users_for_session = AsyncMock(return_value=[(9, "@5", 42)])
        mock_session = MagicMock()
        transcript = tmp_path / "session.jsonl"
        transcript.write_text('"{}"', encoding="utf-8")
        mock_session.file_path = str(transcript)
        mock_sm.resolve_session_for_window = AsyncMock(return_value=mock_session)
        mock_sm.update_user_window_offset = MagicMock()

        await handle_new_message(completion_msg, mock_bot)

    call = mock_enqueue.await_args.kwargs
    assert call["user_id"] == 9
    assert call["window_id"] == "@5"
    assert call["thread_id"] == 42
    assert call["session_id"] == "s1"
    assert call["turn_id"] == 7
    assert "Codex turn complete" in call["completion_text"]


@pytest.mark.asyncio
async def test_handle_new_message_dedupes_completion_by_session_turn(tmp_path) -> None:
    await mq.shutdown_workers()
    bot = AsyncMock()
    completion_msg = NewMessage(
        session_id="s1",
        text="",
        is_complete=True,
        message_type="completion",
        turn_id=7,
    )

    with (
        patch("codexbot.bot.session_manager") as mock_sm,
        patch(
            "codexbot.handlers.message_queue._process_content_task",
            new_callable=AsyncMock,
        ) as mock_process,
        patch("codexbot.bot.get_interactive_msg_id", return_value=None),
    ):
        mock_sm.find_users_for_session = AsyncMock(return_value=[(9, "@5", 42)])
        mock_session = MagicMock()
        transcript = tmp_path / "session.jsonl"
        transcript.write_text('"{}"', encoding="utf-8")
        mock_session.file_path = str(transcript)
        mock_sm.resolve_session_for_window = AsyncMock(return_value=mock_session)
        mock_sm.update_user_window_offset = MagicMock()

        await handle_new_message(completion_msg, bot)
        await handle_new_message(completion_msg, bot)

        queue = mq.get_message_queue(9, thread_id=42)
        assert queue is not None
        await queue.join()

    assert mock_process.await_count == 1
    assert mq._completion_complete_turns[(9, 42)]["s1"] == {7}
    assert mq._completion_pending_turns.get((9, 42), {}) == {}


@pytest.mark.asyncio
async def test_handle_new_message_stale_completion_marks_warning_text(tmp_path) -> None:
    completion_msg = NewMessage(
        session_id="s1",
        text="",
        is_complete=True,
        message_type="completion",
        turn_id=7,
        is_stale_turn=True,
        turn_had_visible_output=True,
    )

    with (
        patch("codexbot.bot.session_manager") as mock_sm,
        patch(
            "codexbot.bot.enqueue_completion_message", new_callable=AsyncMock
        ) as mock_enqueue,
        patch("codexbot.bot.get_interactive_msg_id", return_value=None),
    ):
        mock_sm.find_users_for_session = AsyncMock(return_value=[(9, "@5", 42)])
        mock_session = MagicMock()
        transcript = tmp_path / "session.jsonl"
        transcript.write_text('"{}"', encoding="utf-8")
        mock_session.file_path = str(transcript)
        mock_sm.resolve_session_for_window = AsyncMock(return_value=mock_session)
        mock_sm.update_user_window_offset = MagicMock()

        await handle_new_message(completion_msg, AsyncMock())

    completion_text = mock_enqueue.await_args.kwargs["completion_text"]
    assert "⚠ This completion arrived after a newer turn started." in completion_text


@pytest.mark.asyncio
async def test_handle_new_message_request_user_input_uses_interactive_ui(
    tmp_path,
) -> None:
    tool_msg = NewMessage(
        session_id="s1",
        text="**request_user_input**(Continue?)",
        is_complete=True,
        message_type="content",
        content_type="tool_use",
        role="assistant",
        tool_name="request_user_input",
    )

    with (
        patch("codexbot.bot.session_manager") as mock_sm,
        patch("codexbot.bot.set_interactive_mode") as mock_set_interactive_mode,
        patch("codexbot.bot.clear_interactive_mode") as mock_clear_interactive_mode,
        patch("codexbot.bot.handle_interactive_ui", new_callable=AsyncMock) as mock_ui,
        patch("codexbot.bot.get_message_queue", return_value=None),
        patch("codexbot.bot.asyncio.sleep", new_callable=AsyncMock),
        patch(
            "codexbot.bot.enqueue_status_update", new_callable=AsyncMock
        ) as mock_status,
        patch(
            "codexbot.bot.enqueue_content_message", new_callable=AsyncMock
        ) as mock_content,
    ):
        mock_sm.find_users_for_session = AsyncMock(return_value=[(9, "@5", 42)])
        mock_ui.return_value = True
        mock_session = MagicMock()
        transcript = tmp_path / "session.jsonl"
        transcript.write_text('"{}"', encoding="utf-8")
        mock_session.file_path = str(transcript)
        mock_sm.resolve_session_for_window = AsyncMock(return_value=mock_session)
        mock_sm.update_user_window_offset = MagicMock()

        await handle_new_message(tool_msg, AsyncMock())

    mock_set_interactive_mode.assert_called_once_with(9, "@5", 42)
    mock_ui.assert_awaited_once()
    mock_clear_interactive_mode.assert_not_called()
    mock_status.assert_not_awaited()
    mock_content.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_new_message_request_user_input_structured_prompt_uses_buttons(
    tmp_path,
) -> None:
    tool_msg = NewMessage(
        session_id="s1",
        text="**request_user_input**(Continue?)",
        is_complete=True,
        message_type="content",
        content_type="tool_use",
        role="assistant",
        tool_name="request_user_input",
        tool_input={
            "questions": [
                {
                    "question": "Continue?",
                    "options": [
                        {"label": "Yes", "description": "Proceed"},
                        {"label": "No", "description": "Stop"},
                    ],
                }
            ]
        },
    )

    bot = AsyncMock()
    sent_msg = MagicMock()
    sent_msg.message_id = 321
    bot.send_message = AsyncMock(return_value=sent_msg)

    with (
        patch("codexbot.bot.session_manager") as mock_sm,
        patch("codexbot.bot.set_interactive_mode") as mock_set_interactive_mode,
        patch("codexbot.bot.handle_interactive_ui", new_callable=AsyncMock) as mock_ui,
        patch("codexbot.bot.get_interactive_msg_id", return_value=None),
        patch(
            "codexbot.bot.enqueue_status_update", new_callable=AsyncMock
        ) as mock_status,
        patch(
            "codexbot.bot.enqueue_content_message", new_callable=AsyncMock
        ) as mock_content,
    ):
        mock_sm.find_users_for_session = AsyncMock(return_value=[(9, "@5", 42)])
        mock_session = MagicMock()
        transcript = tmp_path / "session.jsonl"
        transcript.write_text('"{}"', encoding="utf-8")
        mock_session.file_path = str(transcript)
        mock_sm.resolve_session_for_window = AsyncMock(return_value=mock_session)
        mock_sm.update_user_window_offset = MagicMock()
        mock_sm.resolve_chat_id.return_value = 100

        await handle_new_message(tool_msg, bot)

    mock_set_interactive_mode.assert_called_once_with(9, "@5", 42)
    mock_ui.assert_not_awaited()
    mock_status.assert_not_awaited()
    mock_content.assert_not_awaited()
    assert bot.send_message.await_count == 1
    assert "Continue?" in bot.send_message.await_args.kwargs["text"]
    assert bot.send_message.await_args.kwargs["reply_markup"] is not None


@pytest.mark.asyncio
async def test_handle_new_message_request_user_input_with_other_option_uses_interactive_ui(
    tmp_path,
) -> None:
    tool_msg = NewMessage(
        session_id="s1",
        text="**request_user_input**(Continue?)",
        is_complete=True,
        message_type="content",
        content_type="tool_use",
        role="assistant",
        tool_name="request_user_input",
        tool_input={
            "questions": [
                {
                    "question": "Continue?",
                    "isOther": True,
                    "options": [
                        {"label": "Yes", "description": "Proceed"},
                        {"label": "No", "description": "Stop"},
                    ],
                }
            ]
        },
    )

    with (
        patch("codexbot.bot.session_manager") as mock_sm,
        patch("codexbot.bot.set_interactive_mode") as mock_set_interactive_mode,
        patch("codexbot.bot.clear_interactive_mode") as mock_clear_interactive_mode,
        patch("codexbot.bot.handle_interactive_ui", new_callable=AsyncMock) as mock_ui,
        patch("codexbot.bot.get_message_queue", return_value=None),
        patch("codexbot.bot.asyncio.sleep", new_callable=AsyncMock),
        patch(
            "codexbot.bot.enqueue_status_update", new_callable=AsyncMock
        ) as mock_status,
        patch(
            "codexbot.bot.enqueue_content_message", new_callable=AsyncMock
        ) as mock_content,
    ):
        mock_sm.find_users_for_session = AsyncMock(return_value=[(9, "@5", 42)])
        mock_ui.return_value = True
        mock_session = MagicMock()
        transcript = tmp_path / "session.jsonl"
        transcript.write_text('"{}"', encoding="utf-8")
        mock_session.file_path = str(transcript)
        mock_sm.resolve_session_for_window = AsyncMock(return_value=mock_session)
        mock_sm.update_user_window_offset = MagicMock()

        await handle_new_message(tool_msg, AsyncMock())

    mock_set_interactive_mode.assert_called_once_with(9, "@5", 42)
    mock_ui.assert_awaited_once()
    mock_clear_interactive_mode.assert_not_called()
    mock_status.assert_not_awaited()
    mock_content.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_new_message_request_user_input_multi_question_uses_interactive_ui(
    tmp_path,
) -> None:
    tool_msg = NewMessage(
        session_id="s1",
        text="**request_user_input**(Choose rollout mode)",
        is_complete=True,
        message_type="content",
        content_type="tool_use",
        role="assistant",
        tool_name="request_user_input",
        tool_input={
            "questions": [
                {
                    "question": "Choose rollout mode",
                    "options": [
                        {"label": "Canary", "description": "Slow rollout"},
                        {"label": "All users", "description": "Immediate rollout"},
                    ],
                },
                {
                    "question": "Choose approval policy",
                    "options": [
                        {"label": "Ask", "description": "Prompt for approval"},
                        {"label": "Auto", "description": "Proceed automatically"},
                    ],
                },
            ]
        },
    )

    with (
        patch("codexbot.bot.session_manager") as mock_sm,
        patch("codexbot.bot.set_interactive_mode") as mock_set_interactive_mode,
        patch("codexbot.bot.clear_interactive_mode") as mock_clear_interactive_mode,
        patch("codexbot.bot.handle_interactive_ui", new_callable=AsyncMock) as mock_ui,
        patch(
            "codexbot.bot.render_interactive_message", new_callable=AsyncMock
        ) as mock_render_fallback,
        patch("codexbot.bot.get_message_queue", return_value=None),
        patch("codexbot.bot.asyncio.sleep", new_callable=AsyncMock),
        patch(
            "codexbot.bot.enqueue_status_update", new_callable=AsyncMock
        ) as mock_status,
        patch(
            "codexbot.bot.enqueue_content_message", new_callable=AsyncMock
        ) as mock_content,
    ):
        mock_sm.find_users_for_session = AsyncMock(return_value=[(9, "@5", 42)])
        mock_ui.return_value = True
        mock_session = MagicMock()
        transcript = tmp_path / "session.jsonl"
        transcript.write_text('"{}"', encoding="utf-8")
        mock_session.file_path = str(transcript)
        mock_sm.resolve_session_for_window = AsyncMock(return_value=mock_session)
        mock_sm.update_user_window_offset = MagicMock()

        await handle_new_message(tool_msg, AsyncMock())

    mock_set_interactive_mode.assert_called_once_with(9, "@5", 42)
    mock_ui.assert_awaited_once()
    mock_render_fallback.assert_not_awaited()
    mock_clear_interactive_mode.assert_not_called()
    mock_status.assert_not_awaited()
    mock_content.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_new_message_exit_plan_mode_uses_interactive_ui(
    tmp_path,
) -> None:
    tool_msg = NewMessage(
        session_id="s1",
        text="**exit_plan_mode**",
        is_complete=True,
        message_type="content",
        content_type="tool_use",
        role="assistant",
        tool_name="exit_plan_mode",
        tool_input={"plan": "Step 1\nStep 2"},
    )

    with (
        patch("codexbot.bot.session_manager") as mock_sm,
        patch("codexbot.bot.set_interactive_mode") as mock_set_interactive_mode,
        patch("codexbot.bot.clear_interactive_mode") as mock_clear_interactive_mode,
        patch("codexbot.bot.handle_interactive_ui", new_callable=AsyncMock) as mock_ui,
        patch(
            "codexbot.bot.render_interactive_message", new_callable=AsyncMock
        ) as mock_render_fallback,
        patch("codexbot.bot.get_message_queue", return_value=None),
        patch("codexbot.bot.asyncio.sleep", new_callable=AsyncMock),
        patch(
            "codexbot.bot.enqueue_status_update", new_callable=AsyncMock
        ) as mock_status,
        patch(
            "codexbot.bot.enqueue_content_message", new_callable=AsyncMock
        ) as mock_content,
    ):
        mock_sm.find_users_for_session = AsyncMock(return_value=[(9, "@5", 42)])
        mock_ui.return_value = True
        mock_session = MagicMock()
        transcript = tmp_path / "session.jsonl"
        transcript.write_text('"{}"', encoding="utf-8")
        mock_session.file_path = str(transcript)
        mock_sm.resolve_session_for_window = AsyncMock(return_value=mock_session)
        mock_sm.update_user_window_offset = MagicMock()

        await handle_new_message(tool_msg, AsyncMock())

    mock_set_interactive_mode.assert_called_once_with(9, "@5", 42)
    mock_ui.assert_awaited_once()
    mock_render_fallback.assert_not_awaited()
    mock_clear_interactive_mode.assert_not_called()
    mock_status.assert_not_awaited()
    mock_content.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_new_message_plan_item_completed_prompt_waits_for_completion(
    tmp_path,
) -> None:
    plan_prompt_msg = NewMessage(
        session_id="s1",
        text="**exit_plan_mode**",
        is_complete=True,
        message_type="content",
        content_type="tool_use",
        role="assistant",
        tool_name="exit_plan_mode",
        tool_input={
            "plan": "Step 1\nStep 2",
            "_source": "item_completed_plan",
            "_defer_until_completion": True,
        },
    )
    completion_msg = NewMessage(
        session_id="s1",
        text="",
        is_complete=True,
        message_type="completion",
        turn_id=7,
    )

    bot = AsyncMock()

    with (
        patch("codexbot.bot.session_manager") as mock_sm,
        patch("codexbot.bot.handle_interactive_ui", new_callable=AsyncMock) as mock_ui,
        patch(
            "codexbot.bot.render_interactive_message", new_callable=AsyncMock
        ) as mock_render_fallback,
        patch(
            "codexbot.bot.enqueue_completion_message", new_callable=AsyncMock
        ) as mock_enqueue_completion,
        patch(
            "codexbot.bot.clear_interactive_msg", new_callable=AsyncMock
        ) as mock_clear,
        patch("codexbot.bot.get_message_queue", return_value=None),
        patch("codexbot.bot.asyncio.sleep", new_callable=AsyncMock),
    ):
        mock_sm.find_users_for_session = AsyncMock(return_value=[(9, "@5", 42)])
        mock_ui.return_value = False
        mock_render_fallback.return_value = True
        mock_session = MagicMock()
        transcript = tmp_path / "session.jsonl"
        transcript.write_text('"{}"', encoding="utf-8")
        mock_session.file_path = str(transcript)
        mock_sm.resolve_session_for_window = AsyncMock(return_value=mock_session)
        mock_sm.update_user_window_offset = MagicMock()

        await handle_new_message(plan_prompt_msg, bot)

        assert (9, 42) in bot_mod._pending_post_completion_interactive
        mock_ui.assert_not_awaited()
        mock_render_fallback.assert_not_awaited()

        await handle_new_message(completion_msg, bot)

    mock_ui.assert_awaited_once_with(bot, 9, "@5", 42)
    mock_render_fallback.assert_awaited_once()
    fallback_text = mock_render_fallback.await_args.args[3]
    assert "Codex is waiting for a plan decision." in fallback_text
    assert "Yes, implement this plan" in fallback_text
    assert "No, stay in Plan mode" in fallback_text
    mock_enqueue_completion.assert_not_awaited()
    mock_clear.assert_not_awaited()
    assert (9, 42) not in bot_mod._pending_post_completion_interactive


@pytest.mark.asyncio
async def test_handle_new_message_interactive_tool_uses_fallback_controls_when_ui_parse_misses(
    tmp_path,
) -> None:
    tool_msg = NewMessage(
        session_id="s1",
        text="**exit_plan_mode**",
        is_complete=True,
        message_type="content",
        content_type="tool_use",
        role="assistant",
        tool_name="exit_plan_mode",
        tool_input={"plan": "Step 1\nStep 2"},
    )

    with (
        patch("codexbot.bot.session_manager") as mock_sm,
        patch("codexbot.bot.set_interactive_mode") as mock_set_interactive_mode,
        patch("codexbot.bot.clear_interactive_mode") as mock_clear_interactive_mode,
        patch("codexbot.bot.handle_interactive_ui", new_callable=AsyncMock) as mock_ui,
        patch(
            "codexbot.bot.render_interactive_message", new_callable=AsyncMock
        ) as mock_render_fallback,
        patch("codexbot.bot.get_message_queue", return_value=None),
        patch("codexbot.bot.asyncio.sleep", new_callable=AsyncMock),
        patch(
            "codexbot.bot.enqueue_status_update", new_callable=AsyncMock
        ) as mock_status,
        patch(
            "codexbot.bot.enqueue_content_message", new_callable=AsyncMock
        ) as mock_content,
    ):
        mock_sm.find_users_for_session = AsyncMock(return_value=[(9, "@5", 42)])
        mock_ui.return_value = False
        mock_render_fallback.return_value = True
        mock_session = MagicMock()
        transcript = tmp_path / "session.jsonl"
        transcript.write_text('"{}"', encoding="utf-8")
        mock_session.file_path = str(transcript)
        mock_sm.resolve_session_for_window = AsyncMock(return_value=mock_session)
        mock_sm.update_user_window_offset = MagicMock()

        await handle_new_message(tool_msg, AsyncMock())

    mock_set_interactive_mode.assert_called_once_with(9, "@5", 42)
    mock_ui.assert_awaited_once()
    mock_render_fallback.assert_awaited_once()
    assert "plan decision" in mock_render_fallback.await_args.args[3]
    mock_clear_interactive_mode.assert_not_called()
    mock_status.assert_not_awaited()
    mock_content.assert_not_awaited()


@pytest.mark.asyncio
async def test_prompt_callback_select_sends_selection_number() -> None:
    update = _make_callback_update(f"{CB_PROMPT_SELECT}1", thread_id=42)
    update.callback_query.message.message_id = 999
    context = _make_context()

    bot_mod._interactive_prompt_state[(1, 42)] = {
        "window_id": "@5",
        "mode": "single_request_user_input",
        "question": "Pick one",
        "options": [
            {"label": "Alpha", "description": ""},
            {"label": "Beta", "description": ""},
        ],
        "message_id": 999,
        "page": 0,
    }

    with (
        patch("codexbot.bot.is_user_allowed", return_value=True),
        patch(
            "codexbot.bot.session_manager.is_window_bound_to_thread", return_value=True
        ),
        patch(
            "codexbot.bot.session_manager.send_to_window",
            new_callable=AsyncMock,
            return_value=(True, "sent"),
        ) as mock_send,
        patch("codexbot.bot.safe_edit", new_callable=AsyncMock) as mock_edit,
        patch("codexbot.bot.get_message_queue", return_value=None),
    ):
        await callback_handler(update, context)

    mock_send.assert_awaited_once_with("@5", "2")
    assert "Selected option 2" in mock_edit.await_args.args[1]
    assert (1, 42) not in bot_mod._interactive_prompt_state


@pytest.mark.asyncio
async def test_prompt_callback_page_updates_prompt_page() -> None:
    update = _make_callback_update(f"{CB_PROMPT_PAGE}1", thread_id=42)
    context = _make_context()

    bot_mod._interactive_prompt_state[(1, 42)] = {
        "window_id": "@5",
        "mode": "single_request_user_input",
        "question": "Pick one",
        "options": [
            {"label": "One", "description": ""},
            {"label": "Two", "description": ""},
            {"label": "Three", "description": ""},
            {"label": "Four", "description": ""},
            {"label": "Five", "description": ""},
            {"label": "Six", "description": ""},
            {"label": "Seven", "description": ""},
        ],
        "message_id": 999,
        "page": 0,
    }

    with (
        patch("codexbot.bot.is_user_allowed", return_value=True),
        patch("codexbot.bot.safe_edit", new_callable=AsyncMock) as mock_edit,
    ):
        await callback_handler(update, context)

    assert "Page 2/2" in mock_edit.await_args.args[1]
    assert bot_mod._interactive_prompt_state[(1, 42)]["page"] == 1


@pytest.mark.asyncio
async def test_prompt_callback_cancel_clears_prompt_state() -> None:
    update = _make_callback_update(CB_PROMPT_CANCEL, thread_id=42)
    context = _make_context()

    bot_mod._interactive_prompt_state[(1, 42)] = {
        "window_id": "@5",
        "mode": "single_request_user_input",
        "question": "Pick one",
        "options": [{"label": "One", "description": ""}],
        "message_id": 999,
        "page": 0,
    }

    with (
        patch("codexbot.bot.is_user_allowed", return_value=True),
        patch(
            "codexbot.bot.tmux_manager.find_window_by_id", new_callable=AsyncMock
        ) as mock_find,
        patch(
            "codexbot.bot.tmux_manager.send_keys", new_callable=AsyncMock
        ) as mock_send_keys,
        patch("codexbot.bot.safe_edit", new_callable=AsyncMock) as mock_edit,
    ):
        mock_window = MagicMock()
        mock_window.window_id = "@5"
        mock_find.return_value = mock_window
        await callback_handler(update, context)

    mock_send_keys.assert_awaited_once_with("@5", "Escape", enter=False, literal=False)
    mock_edit.assert_awaited_once()
    assert (1, 42) not in bot_mod._interactive_prompt_state


@pytest.mark.asyncio
async def test_handle_new_message_progress_content_updates_status_not_content(
    tmp_path,
) -> None:
    tool_msg = NewMessage(
        session_id="s1",
        text="**Read**(src/codexbot/bot.py)",
        is_complete=True,
        message_type="content",
        content_type="tool_use",
        role="assistant",
        tool_name="Read",
    )

    with (
        patch("codexbot.bot.session_manager") as mock_sm,
        patch(
            "codexbot.bot.enqueue_status_update", new_callable=AsyncMock
        ) as mock_status,
        patch(
            "codexbot.bot.enqueue_content_message", new_callable=AsyncMock
        ) as mock_content,
        patch("codexbot.bot.get_interactive_msg_id", return_value=None),
    ):
        mock_sm.find_users_for_session = AsyncMock(return_value=[(9, "@5", 42)])
        mock_session = MagicMock()
        transcript = tmp_path / "session.jsonl"
        transcript.write_text('"{}"', encoding="utf-8")
        mock_session.file_path = str(transcript)
        mock_sm.resolve_session_for_window = AsyncMock(return_value=mock_session)
        mock_sm.update_user_window_offset = MagicMock()

        await handle_new_message(tool_msg, AsyncMock())

    mock_status.assert_awaited_once()
    assert mock_status.await_args.args[2] == "@5"
    assert mock_status.await_args.kwargs["thread_id"] == 42
    assert mock_status.await_args.args[3] == "**Read**(src/codexbot/bot.py)"
    mock_content.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_new_message_hides_claude_tool_result_progress(
    tmp_path,
) -> None:
    tool_msg = NewMessage(
        session_id="s1",
        text="**Bash**(pwd)\n  ⎿  Output 2 lines",
        is_complete=True,
        message_type="content",
        content_type="tool_result",
        role="assistant",
        tool_name="Bash",
    )

    with (
        patch("codexbot.bot.session_manager") as mock_sm,
        patch(
            "codexbot.bot.enqueue_status_update", new_callable=AsyncMock
        ) as mock_status,
        patch(
            "codexbot.bot.enqueue_content_message", new_callable=AsyncMock
        ) as mock_content,
        patch("codexbot.bot.get_interactive_msg_id", return_value=None),
    ):
        mock_sm.find_users_for_session = AsyncMock(return_value=[(9, "@5", 42)])
        mock_sm.get_window_state.return_value = MagicMock(runtime="claude")
        mock_session = MagicMock()
        transcript = tmp_path / "session.jsonl"
        transcript.write_text('"{}"', encoding="utf-8")
        mock_session.file_path = str(transcript)
        mock_sm.resolve_session_for_window = AsyncMock(return_value=mock_session)
        mock_sm.update_user_window_offset = MagicMock()

        await handle_new_message(tool_msg, AsyncMock())

    mock_status.assert_not_awaited()
    mock_content.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_new_message_text_content_enqueues_separate_final_message(
    tmp_path,
) -> None:
    text_msg = NewMessage(
        session_id="s1",
        text="Final answer",
        is_complete=True,
        message_type="content",
        content_type="text",
        role="assistant",
    )

    with (
        patch("codexbot.bot.session_manager") as mock_sm,
        patch(
            "codexbot.bot.enqueue_status_update", new_callable=AsyncMock
        ) as mock_status,
        patch(
            "codexbot.bot.enqueue_content_message", new_callable=AsyncMock
        ) as mock_content,
        patch("codexbot.bot.get_interactive_msg_id", return_value=None),
    ):
        mock_sm.find_users_for_session = AsyncMock(return_value=[(9, "@5", 42)])
        mock_session = MagicMock()
        transcript = tmp_path / "session.jsonl"
        transcript.write_text('"{}"', encoding="utf-8")
        mock_session.file_path = str(transcript)
        mock_sm.resolve_session_for_window = AsyncMock(return_value=mock_session)
        mock_sm.update_user_window_offset = MagicMock()

        await handle_new_message(text_msg, AsyncMock())

    mock_status.assert_not_awaited()
    mock_content.assert_awaited_once()
    assert mock_content.await_args.kwargs["window_id"] == "@5"
    assert mock_content.await_args.kwargs["thread_id"] == 42
    assert mock_content.await_args.kwargs["convert_status_to_content"] is False
