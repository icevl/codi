"""Tests for SessionManager pure dict operations."""

import time
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from codexbot.session import CodexSession, SessionManager


@pytest.fixture
def mgr(monkeypatch) -> SessionManager:
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
    return SessionManager()


class TestThreadBindings:
    def test_bind_and_get(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1")
        assert mgr.get_window_for_thread(100, 1) == "@1"

    def test_bind_unbind_get_returns_none(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1")
        mgr.unbind_thread(100, 1)
        assert mgr.get_window_for_thread(100, 1) is None

    def test_unbind_nonexistent_returns_none(self, mgr: SessionManager) -> None:
        assert mgr.unbind_thread(100, 999) is None

    def test_iter_thread_bindings(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1")
        mgr.bind_thread(100, 2, "@2")
        mgr.bind_thread(200, 3, "@3")
        result = set(mgr.iter_thread_bindings())
        assert result == {(100, 1, "@1"), (100, 2, "@2"), (200, 3, "@3")}


class TestGroupChatId:
    """Tests for group chat_id routing (supergroup forum topic support).

    IMPORTANT: These tests protect against regression. The group_chat_ids
    mapping is required for Telegram supergroup forum topics — without it,
    all outbound messages fail with "Message thread not found". This was
    erroneously removed once (26cb81f) and restored in PR #23. Do NOT
    delete these tests or the underlying functionality.
    """

    def test_resolve_with_stored_group_id(self, mgr: SessionManager) -> None:
        """resolve_chat_id returns stored group chat_id for known thread."""
        mgr.set_group_chat_id(100, 1, -1001234567890)
        assert mgr.resolve_chat_id(100, 1) == -1001234567890

    def test_resolve_without_group_id_falls_back_to_user_id(
        self, mgr: SessionManager
    ) -> None:
        """resolve_chat_id falls back to user_id when no group_id stored."""
        assert mgr.resolve_chat_id(100, 1) == 100

    def test_resolve_none_thread_id_falls_back_to_user_id(
        self, mgr: SessionManager
    ) -> None:
        """resolve_chat_id returns user_id when thread_id is None (private chat)."""
        mgr.set_group_chat_id(100, 1, -1001234567890)
        assert mgr.resolve_chat_id(100) == 100

    def test_set_group_chat_id_overwrites(self, mgr: SessionManager) -> None:
        """set_group_chat_id updates the stored value on change."""
        mgr.set_group_chat_id(100, 1, -999)
        mgr.set_group_chat_id(100, 1, -888)
        assert mgr.resolve_chat_id(100, 1) == -888

    def test_multiple_threads_independent(self, mgr: SessionManager) -> None:
        """Different threads for the same user store independent group chat_ids."""
        mgr.set_group_chat_id(100, 1, -111)
        mgr.set_group_chat_id(100, 2, -222)
        assert mgr.resolve_chat_id(100, 1) == -111
        assert mgr.resolve_chat_id(100, 2) == -222

    def test_multiple_users_independent(self, mgr: SessionManager) -> None:
        """Different users store independent group chat_ids."""
        mgr.set_group_chat_id(100, 1, -111)
        mgr.set_group_chat_id(200, 1, -222)
        assert mgr.resolve_chat_id(100, 1) == -111
        assert mgr.resolve_chat_id(200, 1) == -222

    def test_set_group_chat_id_with_none_thread(self, mgr: SessionManager) -> None:
        """set_group_chat_id handles None thread_id (mapped to 0)."""
        mgr.set_group_chat_id(100, None, -999)
        # thread_id=None in resolve falls back to user_id (by design)
        assert mgr.resolve_chat_id(100, None) == 100
        # The stored key is "100:0", only accessible with explicit thread_id=0
        assert mgr.group_chat_ids.get("100:0") == -999


class TestWindowState:
    def test_get_creates_new(self, mgr: SessionManager) -> None:
        state = mgr.get_window_state("@0")
        assert state.session_id == ""
        assert state.cwd == ""

    def test_get_returns_existing(self, mgr: SessionManager) -> None:
        state = mgr.get_window_state("@1")
        state.session_id = "abc"
        assert mgr.get_window_state("@1").session_id == "abc"

    def test_clear_window_session(self, mgr: SessionManager) -> None:
        state = mgr.get_window_state("@1")
        state.session_id = "abc"
        mgr.clear_window_session("@1")
        assert mgr.get_window_state("@1").session_id == ""
        assert "@1" in mgr._suppress_cwd_fallback_windows
        assert "@1" in mgr._force_status_probe_windows

    def test_runtime_defaults_to_codex(self, mgr: SessionManager) -> None:
        state = mgr.get_window_state("@0")
        assert state.runtime == "codex"


class TestClaudeTranscriptPath:
    """Tests for Claude transcript path encoding."""

    def test_encodes_slashes_and_dots(self) -> None:
        from codexbot.session import claude_transcript_path

        p = claude_transcript_path(
            "11111111-2222-3333-4444-555555555555",
            "/Users/x/proj/.claude/worktree/foo",
        )
        assert p is not None
        # both '/' and '.' must be replaced with '-'
        assert p.parent.name == "-Users-x-proj--claude-worktree-foo"
        assert p.name == "11111111-2222-3333-4444-555555555555.jsonl"

    def test_empty_inputs_return_none(self) -> None:
        from codexbot.session import claude_transcript_path

        assert claude_transcript_path("", "/tmp") is None
        assert claude_transcript_path("sid", "") is None


class TestResolveWindowForThread:
    def test_none_thread_id_returns_none(self, mgr: SessionManager) -> None:
        assert mgr.resolve_window_for_thread(100, None) is None

    def test_unbound_thread_returns_none(self, mgr: SessionManager) -> None:
        assert mgr.resolve_window_for_thread(100, 42) is None

    def test_bound_thread_returns_window(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 42, "@3")
        assert mgr.resolve_window_for_thread(100, 42) == "@3"


class TestDisplayNames:
    def test_get_display_name_fallback(self, mgr: SessionManager) -> None:
        """get_display_name returns window_id when no display name is set."""
        assert mgr.get_display_name("@99") == "@99"

    def test_set_and_get_display_name(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1", window_name="myproject")
        assert mgr.get_display_name("@1") == "myproject"

    def test_set_display_name_update(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1", window_name="old-name")
        mgr.window_display_names["@1"] = "new-name"
        assert mgr.get_display_name("@1") == "new-name"

    def test_bind_thread_sets_display_name(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1", window_name="proj")
        assert mgr.get_display_name("@1") == "proj"

    def test_bind_thread_without_name_no_display(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1")
        # No display name set, fallback to window_id
        assert mgr.get_display_name("@1") == "@1"


class TestIsWindowId:
    def test_valid_ids(self, mgr: SessionManager) -> None:
        assert mgr._is_window_id("@0") is True
        assert mgr._is_window_id("@12") is True
        assert mgr._is_window_id("@999") is True

    def test_invalid_ids(self, mgr: SessionManager) -> None:
        assert mgr._is_window_id("myproject") is False
        assert mgr._is_window_id("@") is False
        assert mgr._is_window_id("") is False
        assert mgr._is_window_id("@abc") is False


class TestWindowBindingGuards:
    def test_is_window_bound_to_thread(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1")
        assert mgr.is_window_bound_to_thread(100, 1, "@1") is True
        assert mgr.is_window_bound_to_thread(100, 1, "@2") is False

    def test_is_window_not_bound_to_thread_when_unknown(
        self, mgr: SessionManager
    ) -> None:
        assert mgr.is_window_bound_to_thread(100, 1, "@1") is False

    @pytest.mark.asyncio
    async def test_resolve_stale_ids_cleans_dead_thread_binding(
        self, mgr: SessionManager
    ) -> None:
        mgr.bind_thread(100, 1, "@dead")

        with patch(
            "codexbot.session.tmux_manager.list_windows",
            new=AsyncMock(return_value=[]),
        ):
            await mgr.resolve_stale_ids()

        assert mgr.get_window_for_thread(100, 1) is None


class TestRefreshWindowSession:
    @pytest.mark.asyncio
    async def test_claude_shell_window_clears_stale_session(
        self,
        mgr: SessionManager,
    ) -> None:
        ws = mgr.get_window_state("@1")
        ws.session_id = "stale-claude-session"
        ws.cwd = "/tmp"
        ws.window_name = "codexbot"
        ws.runtime = "claude"

        shell_window = type(
            "Window",
            (),
            {
                "window_id": "@1",
                "window_name": "codexbot",
                "cwd": "/tmp",
                "pane_current_command": "zsh",
            },
        )()

        with patch(
            "codexbot.session.tmux_manager.find_window_by_id",
            new=AsyncMock(return_value=shell_window),
        ):
            resolved = await mgr.refresh_window_session_if_stale("@1")

        assert resolved is None
        assert mgr.get_window_state("@1").session_id == ""

    @pytest.mark.asyncio
    async def test_claude_rebinds_when_transcript_missing(
        self,
        mgr: SessionManager,
    ) -> None:
        ws = mgr.get_window_state("@1")
        ws.session_id = "stale-claude-session"
        ws.cwd = "/tmp"
        ws.window_name = "codexbot"
        ws.runtime = "claude"

        mock_runtime = MagicMock()
        mock_runtime.discover_session_id = AsyncMock(
            return_value="fresh-claude-session"
        )

        with (
            patch.object(mgr, "_refresh_sessions_index", new=AsyncMock()),
            patch("codexbot.session.get_runtime", return_value=mock_runtime),
            patch(
                "codexbot.session.tmux_manager.find_window_by_id",
                new=AsyncMock(
                    return_value=type(
                        "Window",
                        (),
                        {
                            "window_id": "@1",
                            "window_name": "codexbot",
                            "cwd": "/tmp",
                            "pane_current_command": "claude",
                        },
                    )()
                ),
            ),
            patch(
                "codexbot.session.tmux_manager.get_pane_pid",
                new=AsyncMock(return_value=1234),
            ),
        ):
            mgr._session_index = {}
            resolved = await mgr.refresh_window_session_if_stale("@1")

        assert resolved == "fresh-claude-session"
        assert mgr.get_window_state("@1").session_id == "fresh-claude-session"
        mock_runtime.discover_session_id.assert_awaited_once_with(
            window_id="@1",
            pane_pid=1234,
            cwd="/tmp",
            allow_cwd_fallback=False,
        )

    @pytest.mark.asyncio
    async def test_rebinds_when_transcript_missing(
        self,
        mgr: SessionManager,
        tmp_path,
    ) -> None:
        ws = mgr.get_window_state("@1")
        ws.session_id = "old-session"
        ws.cwd = "/tmp"
        ws.window_name = "codexbot"

        norm_cwd = mgr._normalize_cwd("/tmp")
        new_transcript = tmp_path / "new.jsonl"
        new_transcript.write_text("{}", encoding="utf-8")

        with patch.object(mgr, "_refresh_sessions_index", new=AsyncMock()):
            mgr._session_index = {}
            mgr._session_cwd_index = {"new-session": norm_cwd}
            mgr._session_mtime_index = {"new-session": time.time()}
            resolved = await mgr.refresh_window_session_if_stale("@1")

        assert resolved == "new-session"
        assert mgr.get_window_state("@1").session_id == "new-session"

    @pytest.mark.asyncio
    async def test_rebinds_when_waiting_for_fresh_session_after_clear(
        self,
        mgr: SessionManager,
        tmp_path,
    ) -> None:
        ws = mgr.get_window_state("@1")
        ws.session_id = "old-session"
        ws.cwd = "/tmp"
        ws.window_name = "codexbot"

        old_transcript = tmp_path / "old.jsonl"
        old_transcript.write_text("{}", encoding="utf-8")
        new_transcript = tmp_path / "new.jsonl"
        new_transcript.write_text("{}", encoding="utf-8")
        norm_cwd = mgr._normalize_cwd("/tmp")
        mgr.clear_window_session("@1")

        with patch.object(mgr, "_refresh_sessions_index", new=AsyncMock()):
            mgr._session_index = {"old-session": old_transcript}
            mgr._session_cwd_index = {
                "old-session": norm_cwd,
                "new-session": norm_cwd,
            }
            mgr._session_mtime_index = {
                "old-session": time.time() - 3600,
                "new-session": time.time(),
            }
            resolved = await mgr.refresh_window_session_if_stale("@1")

        assert resolved == "new-session"
        assert mgr.get_window_state("@1").session_id == "new-session"

    @pytest.mark.asyncio
    async def test_refresh_is_throttled_per_window(
        self,
        mgr: SessionManager,
        monkeypatch,
        tmp_path,
    ) -> None:
        ws = mgr.get_window_state("@1")
        ws.session_id = "old-session"
        ws.cwd = "/tmp"
        ws.window_name = "codexbot"
        mgr.mark_window_for_new_session("@1", clear_existing=False)
        transcript = tmp_path / "old.jsonl"
        transcript.write_text("{}", encoding="utf-8")

        monkeypatch.setattr(
            "codexbot.session.config.status_probe_min_interval_seconds",
            60.0,
        )

        with (
            patch.object(mgr, "_refresh_sessions_index", new=AsyncMock()),
            patch.object(
                mgr,
                "_fresh_session_id_for_window",
                new=AsyncMock(return_value=None),
            ) as mock_refresh,
        ):
            mgr._session_index = {"old-session": transcript}
            first = await mgr.refresh_window_session_if_stale("@1")
            second = await mgr.refresh_window_session_if_stale("@1")

        assert first == "old-session"
        assert second == "old-session"
        mock_refresh.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_keeps_current_session_when_recent_and_not_waiting_for_new_session(
        self,
        mgr: SessionManager,
        tmp_path,
    ) -> None:
        ws = mgr.get_window_state("@1")
        ws.session_id = "stable-session"
        ws.cwd = "/tmp"
        ws.window_name = "codexbot"

        transcript = tmp_path / "stable.jsonl"
        transcript.write_text("{}", encoding="utf-8")

        with (
            patch.object(mgr, "_refresh_sessions_index", new=AsyncMock()),
            patch.object(
                mgr,
                "_fresh_session_id_for_window",
                new=AsyncMock(return_value="new-session"),
            ) as mock_refresh,
        ):
            mgr._session_index = {"stable-session": transcript}
            resolved = await mgr.refresh_window_session_if_stale("@1")

        assert resolved == "stable-session"
        assert mgr.get_window_state("@1").session_id == "stable-session"
        mock_refresh.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_force_refresh_rebinds_even_when_transcript_recent(
        self,
        mgr: SessionManager,
        tmp_path,
    ) -> None:
        ws = mgr.get_window_state("@1")
        ws.session_id = "stable-session"
        ws.cwd = "/tmp"
        ws.window_name = "codexbot"

        transcript = tmp_path / "stable.jsonl"
        transcript.write_text("{}", encoding="utf-8")

        mgr._force_status_probe_windows = {"@1"}

        with (
            patch.object(mgr, "_refresh_sessions_index", new=AsyncMock()),
            patch.object(
                mgr,
                "_fresh_session_id_for_window",
                new=AsyncMock(return_value="new-session"),
            ) as mock_refresh,
        ):
            mgr._session_index = {"stable-session": transcript}
            resolved = await mgr.refresh_window_session_if_stale("@1")

        assert resolved == "new-session"
        assert mgr.get_window_state("@1").session_id == "new-session"
        mock_refresh.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_to_window_does_not_mark_force_probe_for_regular_text(
        self,
        mgr: SessionManager,
    ) -> None:
        window = type("Window", (), {"window_id": "@1"})()

        with (
            patch(
                "codexbot.session.tmux_manager.find_window_by_id",
                new=AsyncMock(return_value=window),
            ),
            patch(
                "codexbot.session.tmux_manager.send_keys",
                new=AsyncMock(return_value=True),
            ),
        ):
            ok, _msg = await mgr.send_to_window("@1", "hi")

        assert ok is True
        assert "@1" not in getattr(mgr, "_force_status_probe_windows", set())

    @pytest.mark.asyncio
    async def test_send_to_window_marks_force_probe_for_new_session_commands(
        self,
        mgr: SessionManager,
    ) -> None:
        window = type("Window", (), {"window_id": "@1"})()

        with (
            patch(
                "codexbot.session.tmux_manager.find_window_by_id",
                new=AsyncMock(return_value=window),
            ),
            patch(
                "codexbot.session.tmux_manager.send_keys",
                new=AsyncMock(return_value=True),
            ),
        ):
            ok, _msg = await mgr.send_to_window("@1", "/new")

        assert ok is True
        assert "@1" in mgr._force_status_probe_windows

    @pytest.mark.asyncio
    async def test_find_users_for_session_rebinds_stale_window_before_matching(
        self,
        mgr: SessionManager,
        tmp_path,
    ) -> None:
        mgr.bind_thread(100, 42, "@1")
        ws = mgr.get_window_state("@1")
        ws.session_id = "old-session"
        ws.cwd = "/tmp"
        ws.window_name = "codexbot"

        norm_cwd = mgr._normalize_cwd("/tmp")
        new_transcript = tmp_path / "new.jsonl"
        new_transcript.write_text("{}", encoding="utf-8")

        async def _get_session_direct(
            session_id: str,
            cwd: str | None = None,
        ) -> CodexSession | None:
            if session_id != "new-session":
                return None
            return CodexSession(
                session_id=session_id,
                summary="fresh",
                message_count=1,
                file_path=str(tmp_path / "new.jsonl"),
            )

        with (
            patch.object(mgr, "_refresh_sessions_index", new=AsyncMock()),
            patch.object(
                mgr,
                "_get_session_direct",
                new=AsyncMock(side_effect=_get_session_direct),
            ),
        ):
            mgr._session_index = {}
            mgr._session_cwd_index = {"new-session": norm_cwd}
            mgr._session_mtime_index = {"new-session": time.time()}
            users = await mgr.find_users_for_session("new-session")

        assert users == [(100, "@1", 42)]
        assert mgr.get_window_state("@1").session_id == "new-session"

    @pytest.mark.asyncio
    async def test_send_to_window_rearms_probe_after_clear_reset(
        self,
        mgr: SessionManager,
    ) -> None:
        window = type("Window", (), {"window_id": "@1"})()
        mgr.clear_window_session("@1")
        mgr._force_status_probe_windows.clear()

        with (
            patch(
                "codexbot.session.tmux_manager.find_window_by_id",
                new=AsyncMock(return_value=window),
            ),
            patch(
                "codexbot.session.tmux_manager.send_keys",
                new=AsyncMock(return_value=True),
            ),
        ):
            ok, _msg = await mgr.send_to_window("@1", "continue")

        assert ok is True
        assert "@1" in mgr._force_status_probe_windows


class TestSessionDiscoveryFallback:
    @pytest.mark.asyncio
    async def test_refresh_skips_cwd_fallback_while_clear_reset_pending(
        self,
        mgr: SessionManager,
        tmp_path,
    ) -> None:
        ws = mgr.get_window_state("@1")
        ws.session_id = "old-session"
        ws.cwd = "/tmp"
        ws.window_name = "codexbot"
        mgr.clear_window_session("@1")

        norm_cwd = mgr._normalize_cwd("/tmp")
        old_transcript = tmp_path / "old.jsonl"
        old_transcript.write_text("{}", encoding="utf-8")

        with patch.object(mgr, "_refresh_sessions_index", new=AsyncMock()):
            mgr._session_index = {"old-session": old_transcript}
            mgr._session_cwd_index = {"old-session": norm_cwd}
            mgr._session_mtime_index = {"old-session": time.time() - 3600}
            resolved = await mgr.refresh_window_session_if_stale("@1")

        assert resolved is None
        assert mgr.get_window_state("@1").session_id == ""

    @pytest.mark.asyncio
    async def test_refresh_prefers_cwd_fallback_before_wait_loop(
        self,
        mgr: SessionManager,
    ) -> None:
        ws = mgr.get_window_state("@1")
        ws.session_id = ""
        ws.cwd = "/tmp"
        ws.window_name = "codexbot"

        with (
            patch.object(
                mgr,
                "_fallback_session_id_for_cwd",
                new=AsyncMock(return_value="sid-fallback"),
            ) as mock_fallback,
        ):
            resolved = await mgr.refresh_window_session_if_stale("@1")

        assert resolved == "sid-fallback"
        assert mgr.get_window_state("@1").session_id == "sid-fallback"
        mock_fallback.assert_awaited_once_with("/tmp", exclude_window_id="@1")

    @pytest.mark.asyncio
    async def test_load_session_map_prefers_cwd_fallback_before_wait_loop(
        self,
        mgr: SessionManager,
    ) -> None:
        window = type(
            "Window",
            (),
            {
                "window_id": "@1",
                "window_name": "codexbot",
                "cwd": "/tmp",
            },
        )()

        with (
            patch(
                "codexbot.session.tmux_manager.list_windows",
                new=AsyncMock(return_value=[window]),
            ),
            patch.object(
                mgr,
                "_fallback_session_id_for_cwd",
                new=AsyncMock(return_value="sid-fallback"),
            ) as mock_fallback,
            patch.object(
                mgr,
                "wait_for_session_map_entry",
                new=AsyncMock(return_value=True),
            ) as mock_wait,
        ):
            await mgr.load_session_map()

        assert mgr.get_window_state("@1").session_id == "sid-fallback"
        mock_fallback.assert_awaited_once_with("/private/tmp")
        mock_wait.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_load_session_map_does_not_assign_codex_fallback_to_claude(
        self,
        mgr: SessionManager,
    ) -> None:
        """A claude window with an empty session_id must not receive a
        codex-transcript fallback — that triggers a "clearing claude shell
        window binding" spam loop because refresh_window_session_if_stale
        promptly clears it again."""
        ws = mgr.get_window_state("@1")
        ws.runtime = "claude"
        ws.cwd = "/tmp"
        ws.window_name = "codexbot"
        # session_id intentionally left empty.

        window = type(
            "Window",
            (),
            {
                "window_id": "@1",
                "window_name": "codexbot",
                "cwd": "/tmp",
            },
        )()

        with (
            patch(
                "codexbot.session.tmux_manager.list_windows",
                new=AsyncMock(return_value=[window]),
            ),
            patch.object(
                mgr,
                "_fallback_session_id_for_cwd",
                new=AsyncMock(return_value="sid-codex-fallback"),
            ) as mock_fallback,
            patch.object(
                mgr,
                "wait_for_session_map_entry",
                new=AsyncMock(return_value=True),
            ) as mock_wait,
        ):
            await mgr.load_session_map()

        assert mgr.get_window_state("@1").session_id == ""
        mock_fallback.assert_not_awaited()
        mock_wait.assert_not_awaited()
