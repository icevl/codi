"""Tests for interactive_ui — handle_interactive_ui and keyboard layout."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from codexbot.handlers.interactive_ui import (
    _build_interactive_keyboard,
    handle_interactive_ui,
    is_interactive_tool_name,
)
from codexbot.handlers.callback_data import (
    CB_ASK_DOWN,
    CB_ASK_ENTER,
    CB_ASK_ESC,
    CB_ASK_LEFT,
    CB_ASK_PGDN,
    CB_ASK_PGUP,
    CB_ASK_RIGHT,
    CB_ASK_SPACE,
    CB_ASK_TAB,
    CB_ASK_UP,
)


@pytest.fixture
def mock_bot():
    bot = AsyncMock()
    sent_msg = MagicMock()
    sent_msg.message_id = 999
    bot.send_message.return_value = sent_msg
    return bot


@pytest.fixture
def _clear_interactive_state():
    """Ensure interactive state is clean before and after each test."""
    from codexbot.handlers.interactive_ui import _interactive_mode, _interactive_msgs

    _interactive_mode.clear()
    _interactive_msgs.clear()
    yield
    _interactive_mode.clear()
    _interactive_msgs.clear()


@pytest.mark.usefixtures("_clear_interactive_state")
class TestInteractiveStatePersistence:
    def test_save_and_load_round_trip(self, tmp_path, monkeypatch):
        """Mutations to _interactive_msgs survive across module reloads.

        Without this, status polling re-sends every interactive UI on every
        restart because the in-memory msg_id dict is empty and the renderer
        cannot edit the previous message — it sends a new one.
        """
        state_file = tmp_path / "interactive_state.json"

        import codexbot.handlers.interactive_ui as iui

        monkeypatch.setattr(iui, "_INTERACTIVE_STATE_FILE", state_file)

        iui._interactive_msgs[(100, 42)] = 555
        iui._interactive_msgs[(100, 99)] = 777
        iui._save_interactive_msgs()
        assert state_file.exists()

        # Simulate restart: clear in-memory map, reload from disk.
        iui._interactive_msgs.clear()
        reloaded = iui._load_interactive_msgs()
        assert reloaded == {(100, 42): 555, (100, 99): 777}

    def test_load_missing_file_returns_empty(self, tmp_path, monkeypatch):
        import codexbot.handlers.interactive_ui as iui

        monkeypatch.setattr(iui, "_INTERACTIVE_STATE_FILE", tmp_path / "nope.json")
        assert iui._load_interactive_msgs() == {}

    def test_load_corrupted_file_returns_empty(self, tmp_path, monkeypatch):
        import codexbot.handlers.interactive_ui as iui

        path = tmp_path / "broken.json"
        path.write_text("{not valid json", encoding="utf-8")
        monkeypatch.setattr(iui, "_INTERACTIVE_STATE_FILE", path)
        assert iui._load_interactive_msgs() == {}


@pytest.mark.usefixtures("_clear_interactive_state")
class TestHandleInteractiveUI:
    @pytest.mark.asyncio
    async def test_handle_settings_ui_sends_keyboard(
        self, mock_bot: AsyncMock, sample_pane_settings: str
    ):
        """handle_interactive_ui captures Settings pane, sends message with keyboard."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("codexbot.handlers.interactive_ui.tmux_manager") as mock_tmux,
            patch("codexbot.handlers.interactive_ui.session_manager") as mock_sm,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value=sample_pane_settings)
            mock_sm.resolve_chat_id.return_value = 100

            result = await handle_interactive_ui(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

        assert result is True
        mock_bot.send_message.assert_called_once()
        call_kwargs = mock_bot.send_message.call_args
        assert call_kwargs.kwargs["chat_id"] == 100
        assert call_kwargs.kwargs["message_thread_id"] == 42
        assert call_kwargs.kwargs["reply_markup"] is not None

    @pytest.mark.asyncio
    async def test_handle_no_ui_returns_false(self, mock_bot: AsyncMock):
        """Returns False when no interactive UI detected in pane."""
        window_id = "@5"
        mock_window = MagicMock()
        mock_window.window_id = window_id

        with (
            patch("codexbot.handlers.interactive_ui.tmux_manager") as mock_tmux,
            patch("codexbot.handlers.interactive_ui.session_manager"),
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=mock_window)
            mock_tmux.capture_pane = AsyncMock(return_value="$ echo hello\nhello\n$\n")

            result = await handle_interactive_ui(
                mock_bot, user_id=1, window_id=window_id, thread_id=42
            )

        assert result is False
        mock_bot.send_message.assert_not_called()


class TestKeyboardLayoutForSettings:
    def test_settings_keyboard_includes_all_nav_keys(self):
        """Settings keyboard includes Tab, arrows (not vertical_only), Space, Esc, Enter."""
        keyboard = _build_interactive_keyboard("@5", ui_name="Settings")
        # Flatten all callback data values
        all_cb_data = [
            btn.callback_data for row in keyboard.inline_keyboard for btn in row
        ]
        assert any(CB_ASK_TAB in d for d in all_cb_data if d)
        assert any(CB_ASK_SPACE in d for d in all_cb_data if d)
        assert any(CB_ASK_UP in d for d in all_cb_data if d)
        assert any(CB_ASK_DOWN in d for d in all_cb_data if d)
        assert any(CB_ASK_LEFT in d for d in all_cb_data if d)
        assert any(CB_ASK_RIGHT in d for d in all_cb_data if d)
        assert any(CB_ASK_ESC in d for d in all_cb_data if d)
        assert any(CB_ASK_ENTER in d for d in all_cb_data if d)

    def test_ask_user_keyboard_includes_question_navigation(self):
        keyboard = _build_interactive_keyboard("@5", ui_name="AskUserQuestion")
        all_cb_data = [
            btn.callback_data for row in keyboard.inline_keyboard for btn in row
        ]
        assert any(CB_ASK_PGUP in d for d in all_cb_data if d)
        assert any(CB_ASK_PGDN in d for d in all_cb_data if d)


class TestInteractiveToolNameDetection:
    @pytest.mark.parametrize(
        "tool_name",
        ["AskUserQuestion", "request_user_input", "ExitPlanMode", "exit_plan_mode"],
    )
    def test_known_interactive_tool_names(self, tool_name: str):
        assert is_interactive_tool_name(tool_name) is True

    @pytest.mark.parametrize("tool_name", [None, "", "Read", "request-user-input"])
    def test_unknown_interactive_tool_names(self, tool_name: str | None):
        assert is_interactive_tool_name(tool_name) is False
