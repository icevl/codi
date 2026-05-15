"""Tests for forward_command_handler — command forwarding to Codex."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.ext import CommandHandler, MessageHandler


def _make_update(text: str, user_id: int = 1, thread_id: int = 42) -> MagicMock:
    """Build a minimal mock Update with message text in a forum topic."""
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.message = MagicMock()
    update.message.text = text
    update.message.message_thread_id = thread_id
    update.message.chat = MagicMock()
    update.message.chat.send_action = AsyncMock()
    update.effective_chat = MagicMock()
    update.effective_chat.type = "supergroup"
    update.effective_chat.id = 100
    return update


def _make_context() -> MagicMock:
    """Build a minimal mock context."""
    context = MagicMock()
    context.bot = AsyncMock()
    context.user_data = {}
    return context


class TestForwardCommand:
    @pytest.mark.asyncio
    async def test_model_sends_command_to_tmux(self):
        """/model → send_to_window called with "/model"."""
        update = _make_update("/model")
        context = _make_context()

        with (
            patch("codexbot.bot.is_user_allowed", return_value=True),
            patch("codexbot.bot._get_thread_id", return_value=42),
            patch("codexbot.bot.session_manager") as mock_sm,
            patch("codexbot.bot.tmux_manager") as mock_tmux,
            patch("codexbot.bot.safe_reply", new_callable=AsyncMock),
        ):
            mock_sm.resolve_window_for_thread.return_value = "@5"
            mock_sm.get_display_name.return_value = "project"
            mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock())
            mock_sm.send_to_window = AsyncMock(return_value=(True, "ok"))

            from codexbot.bot import forward_command_handler

            await forward_command_handler(update, context)

            mock_sm.send_to_window.assert_called_once_with("@5", "/model")
            update.message.chat.send_action.assert_not_called()

    @pytest.mark.asyncio
    async def test_cost_sends_command_to_tmux(self):
        """/cost → send_to_window called with "/cost"."""
        update = _make_update("/cost")
        context = _make_context()

        with (
            patch("codexbot.bot.is_user_allowed", return_value=True),
            patch("codexbot.bot._get_thread_id", return_value=42),
            patch("codexbot.bot.session_manager") as mock_sm,
            patch("codexbot.bot.tmux_manager") as mock_tmux,
            patch("codexbot.bot.safe_reply", new_callable=AsyncMock),
        ):
            mock_sm.resolve_window_for_thread.return_value = "@5"
            mock_sm.get_display_name.return_value = "project"
            mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock())
            mock_sm.send_to_window = AsyncMock(return_value=(True, "ok"))

            from codexbot.bot import forward_command_handler

            await forward_command_handler(update, context)

            mock_sm.send_to_window.assert_called_once_with("@5", "/cost")

    @pytest.mark.asyncio
    async def test_clear_clears_session(self):
        """/clear → send_to_window + clear_window_session."""
        update = _make_update("/clear")
        context = _make_context()

        with (
            patch("codexbot.bot.is_user_allowed", return_value=True),
            patch("codexbot.bot._get_thread_id", return_value=42),
            patch("codexbot.bot.session_manager") as mock_sm,
            patch("codexbot.bot.tmux_manager") as mock_tmux,
            patch("codexbot.bot.safe_reply", new_callable=AsyncMock),
        ):
            mock_sm.resolve_window_for_thread.return_value = "@5"
            mock_sm.get_display_name.return_value = "project"
            mock_tmux.find_window_by_id = AsyncMock(return_value=MagicMock())
            mock_sm.send_to_window = AsyncMock(return_value=(True, "ok"))

            from codexbot.bot import forward_command_handler

            await forward_command_handler(update, context)

            mock_sm.send_to_window.assert_called_once_with("@5", "/clear")
            mock_sm.clear_window_session.assert_called_once_with("@5")

    @pytest.mark.asyncio
    async def test_status_command_is_handled_locally(self):
        """/status is handled by the bot and never forwarded into Codex."""
        update = _make_update("/status")
        context = _make_context()
        mock_window = MagicMock()
        mock_window.window_id = "@5"
        mock_window.cwd = "/repo"

        with (
            patch("codexbot.bot.is_user_allowed", return_value=True),
            patch("codexbot.bot._get_thread_id", return_value=42),
            patch("codexbot.bot.session_manager") as mock_sm,
            patch("codexbot.bot.tmux_manager") as mock_tmux,
            patch("codexbot.bot.parse_status_line", return_value="Working"),
            patch("codexbot.bot.is_interactive_ui", return_value=False),
            patch("codexbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_sm.resolve_window_for_thread.return_value = "@5"
            mock_sm.get_display_name.return_value = "project"
            mock_sm.get_window_state.return_value = MagicMock(
                session_id="sid-123",
                cwd="/repo",
            )
            mock_sm.resolve_session_for_window = AsyncMock(
                return_value=MagicMock(session_id="sid-123")
            )
            mock_sm.send_to_window = AsyncMock(return_value=(True, "ok"))
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value="pane")

            from codexbot.bot import status_command

            await status_command(update, context)

            mock_sm.send_to_window.assert_not_awaited()
            mock_reply.assert_awaited_once()
            assert "session=sid-123" in mock_reply.await_args.args[1]
            assert "terminal=Working" in mock_reply.await_args.args[1]

    @pytest.mark.asyncio
    async def test_kill_command_kills_window_and_deletes_topic(self):
        """/kill is handled locally: tmux window killed, topic unbound, topic deleted."""
        update = _make_update("/kill")
        context = _make_context()
        mock_window = MagicMock()
        mock_window.window_id = "@5"

        with (
            patch("codexbot.bot.is_user_allowed", return_value=True),
            patch("codexbot.bot._get_thread_id", return_value=42),
            patch("codexbot.bot.session_manager") as mock_sm,
            patch("codexbot.bot.tmux_manager") as mock_tmux,
            patch(
                "codexbot.bot.clear_topic_state", new_callable=AsyncMock
            ) as mock_clear,
            patch("codexbot.bot.safe_reply", new_callable=AsyncMock) as mock_reply,
        ):
            mock_sm.get_window_for_thread.return_value = "@5"
            mock_sm.get_display_name.return_value = "project"
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.kill_window = AsyncMock(return_value=True)

            from codexbot.bot import kill_command

            await kill_command(update, context)

            mock_sm.set_group_chat_id.assert_called_once_with(1, 42, 100)
            mock_tmux.kill_window.assert_awaited_once_with("@5")
            mock_sm.unbind_thread.assert_called_once_with(1, 42)
            mock_clear.assert_awaited_once_with(1, 42, context.bot, context.user_data)
            context.bot.delete_forum_topic.assert_awaited_once_with(
                chat_id=100,
                message_thread_id=42,
            )
            mock_reply.assert_not_awaited()


def test_create_bot_registers_kill_handler_before_forward_command():
    """The dedicated /kill handler must beat the generic command forwarder."""
    from codexbot.bot import create_bot

    app = create_bot()
    handlers = app.handlers[0]

    kill_idx = next(
        i
        for i, handler in enumerate(handlers)
        if isinstance(handler, CommandHandler)
        and handler.commands == frozenset({"kill"})
    )
    forward_idx = next(
        i
        for i, handler in enumerate(handlers)
        if isinstance(handler, MessageHandler)
        and getattr(handler.callback, "__name__", "") == "forward_command_handler"
    )

    assert kill_idx < forward_idx


def test_create_bot_registers_status_handler_before_forward_command():
    """The dedicated /status handler must beat the generic command forwarder."""
    from codexbot.bot import create_bot

    app = create_bot()
    handlers = app.handlers[0]

    status_idx = next(
        i
        for i, handler in enumerate(handlers)
        if isinstance(handler, CommandHandler)
        and handler.commands == frozenset({"status"})
    )
    forward_idx = next(
        i
        for i, handler in enumerate(handlers)
        if isinstance(handler, MessageHandler)
        and getattr(handler.callback, "__name__", "") == "forward_command_handler"
    )

    assert status_idx < forward_idx
