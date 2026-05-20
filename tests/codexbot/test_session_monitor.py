"""Unit tests for SessionMonitor JSONL reading and offset handling."""

import asyncio
import json
import logging
from unittest.mock import AsyncMock, patch

import pytest

from codexbot.monitor_state import TrackedSession
from codexbot.session_monitor import (
    _PARTIAL_JSONL_WARN_RETRY_LIMIT,
    NewMessage,
    SessionInfo,
    SessionMonitor,
)
from codexbot.transcript_parser import ParsedEntry


class TestReadNewLinesOffsetRecovery:
    """Tests for _read_new_lines offset corruption recovery."""

    @pytest.fixture
    def monitor(self, tmp_path):
        """Create a SessionMonitor with temp state file."""
        return SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )

    @pytest.mark.asyncio
    async def test_mid_line_offset_recovery(self, monitor, tmp_path, make_jsonl_entry):
        """Recover from corrupted offset pointing mid-line."""
        # Create JSONL file with two valid lines
        jsonl_file = tmp_path / "session.jsonl"
        entry1 = make_jsonl_entry(msg_type="assistant", content="first message")
        entry2 = make_jsonl_entry(msg_type="assistant", content="second message")
        jsonl_file.write_text(
            json.dumps(entry1) + "\n" + json.dumps(entry2) + "\n",
            encoding="utf-8",
        )

        # Calculate offset pointing into the middle of line 1
        line1_bytes = len(json.dumps(entry1).encode("utf-8")) // 2
        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=line1_bytes,  # Mid-line (corrupted)
        )

        # Read should recover and return empty (offset moved to next line)
        result = await monitor._read_new_lines(session, jsonl_file)

        # Should return empty list (recovery skips to next line, no new content yet)
        assert result == []

        # Offset should now point to start of line 2
        line1_full = len(json.dumps(entry1).encode("utf-8")) + 1  # +1 for newline
        assert session.last_byte_offset == line1_full

    @pytest.mark.asyncio
    async def test_valid_offset_reads_normally(
        self, monitor, tmp_path, make_jsonl_entry
    ):
        """Normal reading when offset points to line start."""
        jsonl_file = tmp_path / "session.jsonl"
        entry1 = make_jsonl_entry(msg_type="assistant", content="first")
        entry2 = make_jsonl_entry(msg_type="assistant", content="second")
        jsonl_file.write_text(
            json.dumps(entry1) + "\n" + json.dumps(entry2) + "\n",
            encoding="utf-8",
        )

        # Offset at 0 should read both lines
        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=0,
        )

        result = await monitor._read_new_lines(session, jsonl_file)

        assert len(result) == 2
        assert session.last_byte_offset == jsonl_file.stat().st_size

    @pytest.mark.asyncio
    async def test_truncation_detection(self, monitor, tmp_path, make_jsonl_entry):
        """Detect file truncation and reset offset."""
        jsonl_file = tmp_path / "session.jsonl"
        entry = make_jsonl_entry(msg_type="assistant", content="content")
        jsonl_file.write_text(json.dumps(entry) + "\n", encoding="utf-8")

        # Set offset beyond file size (simulates truncation)
        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=9999,  # Beyond file size
        )

        result = await monitor._read_new_lines(session, jsonl_file)

        # Should reset offset to 0 and read the line
        assert session.last_byte_offset == jsonl_file.stat().st_size
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_partial_line_warning_is_suppressed_after_retries(
        self, monitor, tmp_path, make_jsonl_entry, caplog: pytest.LogCaptureFixture
    ):
        """Warn only a few times for the same incomplete trailing line."""
        jsonl_file = tmp_path / "session.jsonl"
        entry = make_jsonl_entry(msg_type="assistant", content="first")
        complete_line = json.dumps(entry) + "\n"
        partial_line = '{"type":"assistant","message":{"content":"incomplete"'
        jsonl_file.write_text(complete_line + partial_line, encoding="utf-8")

        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=0,
        )

        attempts = _PARTIAL_JSONL_WARN_RETRY_LIMIT + 3
        with caplog.at_level(logging.INFO):
            for _ in range(attempts):
                await monitor._read_new_lines(session, jsonl_file)

        warning_records = [
            rec
            for rec in caplog.records
            if rec.levelno == logging.WARNING
            and "Partial JSONL line in session" in rec.message
        ]
        info_records = [
            rec
            for rec in caplog.records
            if rec.levelno == logging.INFO
            and "suppressing further warnings until transcript advances" in rec.message
        ]

        assert len(warning_records) == _PARTIAL_JSONL_WARN_RETRY_LIMIT
        assert len(info_records) == 1


class TestSessionMonitorTurnLifecycle:
    @pytest.mark.asyncio
    async def test_completion_emitted_after_user_turn_boundary(
        self, tmp_path, make_jsonl_entry
    ) -> None:
        monitor = SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )
        session_id = "session-turn-test"
        jsonl_file = tmp_path / "session.jsonl"
        jsonl_file.write_text(
            "\n".join(
                [
                    json.dumps(
                        make_jsonl_entry(msg_type="assistant", content="answer")
                    ),
                    json.dumps(
                        make_jsonl_entry(msg_type="user", content="next prompt")
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        monitor.state.update_session(
            TrackedSession(
                session_id=session_id,
                file_path=str(jsonl_file),
                last_byte_offset=0,
            )
        )

        parsed_entries = [
            ParsedEntry(
                role="assistant",
                text="answer",
                content_type="text",
                timestamp="2026-05-20T10:00:00Z",
            ),
            ParsedEntry(
                role="user",
                text="next prompt",
                content_type="text",
                timestamp="2026-05-20T10:00:01Z",
            ),
        ]

        with (
            patch.object(
                monitor,
                "_resolve_active_sessions",
                return_value=[SessionInfo(session_id=session_id, file_path=jsonl_file)],
            ),
            patch.object(
                monitor,
                "_read_new_lines",
                return_value=[{"type": "assistant"}, {"type": "user"}],
            ),
            patch(
                "codexbot.session_monitor.TranscriptParser.parse_entries"
            ) as mock_parse,
        ):
            mock_parse.return_value = (parsed_entries, {})
            messages = await monitor.check_for_updates({session_id}, bootstrap=False)

        assert len(messages) == 3
        assert messages[0].session_id == session_id
        assert messages[0].text == "answer"
        assert messages[0].message_type == "content"
        assert messages[0].timestamp == "2026-05-20T10:00:00Z"

        completion_messages = [
            m
            for m in messages
            if isinstance(m, NewMessage) and m.message_type == "completion"
        ]
        assert len(completion_messages) == 1
        completion = completion_messages[0]
        assert completion.turn_id == 1
        assert completion.message_type == "completion"
        assert completion.turn_had_visible_output
        assert not completion.is_stale_turn
        assert completion.text == ""

    @pytest.mark.asyncio
    async def test_no_completion_for_empty_noop_flow(self, tmp_path) -> None:
        monitor = SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )
        session_id = "session-noop"
        jsonl_file = tmp_path / "session-noop.jsonl"
        jsonl_file.write_text('{"noop": true}\n', encoding="utf-8")

        monitor.state.update_session(
            TrackedSession(
                session_id=session_id,
                file_path=str(jsonl_file),
                last_byte_offset=0,
            )
        )

        parsed_entries = [
            ParsedEntry(
                role="user",
                text="first prompt",
                content_type="text",
                timestamp="",
            )
        ]

        with (
            patch.object(
                monitor,
                "_resolve_active_sessions",
                return_value=[SessionInfo(session_id=session_id, file_path=jsonl_file)],
            ),
            patch.object(monitor, "_read_new_lines", return_value=[{"type": "user"}]),
            patch(
                "codexbot.session_monitor.TranscriptParser.parse_entries"
            ) as mock_parse,
        ):
            mock_parse.return_value = (parsed_entries, {})
            messages = await monitor.check_for_updates({session_id}, bootstrap=False)

        completion_messages = [m for m in messages if m.message_type == "completion"]
        assert completion_messages == []

    @pytest.mark.asyncio
    async def test_stale_completion_marked_when_pending_turn_arrives_late(
        self, tmp_path
    ) -> None:
        monitor = SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )
        session_id = "session-stale"
        jsonl_file = tmp_path / "session-stale.jsonl"
        jsonl_file.write_text('{"stale": true}\n', encoding="utf-8")

        monitor.state.update_session(
            TrackedSession(
                session_id=session_id,
                file_path=str(jsonl_file),
                last_byte_offset=0,
            )
        )

        turn_state = monitor._get_turn_state(session_id)
        turn_state.active_turn_id = 2
        turn_state.next_turn_id = 3
        turn_state.active_turn_had_visible_output = False
        turn_state.pending_completion_turn_id = 1
        turn_state.pending_completion_had_visible_output = True

        parsed_entries = [
            ParsedEntry(
                role="assistant",
                text="newer turn output",
                content_type="text",
                timestamp="",
            )
        ]

        with (
            patch.object(
                monitor,
                "_resolve_active_sessions",
                return_value=[SessionInfo(session_id=session_id, file_path=jsonl_file)],
            ),
            patch.object(
                monitor,
                "_read_new_lines",
                return_value=[{"type": "assistant"}],
            ),
            patch(
                "codexbot.session_monitor.TranscriptParser.parse_entries"
            ) as mock_parse,
        ):
            mock_parse.return_value = (parsed_entries, {})
            messages = await monitor.check_for_updates({session_id}, bootstrap=False)

        assert [m.message_type for m in messages] == ["content", "completion"]
        completion = messages[1]
        assert completion.turn_id == 1
        assert completion.is_stale_turn
        assert completion.turn_had_visible_output

    @pytest.mark.asyncio
    async def test_turn_boundaries_across_consecutive_outputs(self, tmp_path) -> None:
        monitor = SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )
        session_id = "session-turn-boundaries"
        jsonl_file = tmp_path / "session-turn-boundaries.jsonl"
        jsonl_file.write_text('{"turns": true}\n', encoding="utf-8")

        monitor.state.update_session(
            TrackedSession(
                session_id=session_id,
                file_path=str(jsonl_file),
                last_byte_offset=0,
            )
        )

        parsed_entries = [
            ParsedEntry(
                role="assistant",
                text="turn1: first output",
                content_type="text",
                timestamp="",
            ),
            ParsedEntry(
                role="assistant",
                text="turn1: second output",
                content_type="text",
                timestamp="",
            ),
            ParsedEntry(
                role="user",
                text="next prompt",
                content_type="text",
                timestamp="",
            ),
            ParsedEntry(
                role="assistant",
                text="turn2: first output",
                content_type="text",
                timestamp="",
            ),
            ParsedEntry(
                role="user",
                text="follow up",
                content_type="text",
                timestamp="",
            ),
        ]

        with (
            patch.object(
                monitor,
                "_resolve_active_sessions",
                return_value=[SessionInfo(session_id=session_id, file_path=jsonl_file)],
            ),
            patch.object(
                monitor,
                "_read_new_lines",
                return_value=[{"type": "assistant"}] * len(parsed_entries),
            ),
            patch(
                "codexbot.session_monitor.TranscriptParser.parse_entries"
            ) as mock_parse,
        ):
            mock_parse.return_value = (parsed_entries, {})
            messages = await monitor.check_for_updates({session_id}, bootstrap=False)

        completions = [m for m in messages if m.message_type == "completion"]
        assert [m.turn_id for m in completions] == [1, 2]
        assert all(m.turn_had_visible_output for m in completions)

        idx_turn1_last = max(
            i
            for i, m in enumerate(messages)
            if m.message_type == "content" and m.text.startswith("turn1:")
        )
        idx_completion_turn1 = next(
            i
            for i, m in enumerate(messages)
            if m.message_type == "completion" and m.turn_id == 1
        )
        idx_turn2_first = next(
            i
            for i, m in enumerate(messages)
            if m.message_type == "content" and m.text.startswith("turn2:")
        )
        assert idx_turn1_last < idx_completion_turn1 < idx_turn2_first

    @pytest.mark.asyncio
    async def test_dedupes_repeated_completion_markers_for_turn(self, tmp_path) -> None:
        monitor = SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )
        session_id = "session-dedupe"
        jsonl_file = tmp_path / "session-dedupe.jsonl"
        jsonl_file.write_text('{"dedupe": true}\n', encoding="utf-8")

        monitor.state.update_session(
            TrackedSession(
                session_id=session_id,
                file_path=str(jsonl_file),
                last_byte_offset=0,
            )
        )

        parsed_entries = [
            ParsedEntry(
                role="assistant",
                text="turn output",
                content_type="text",
                timestamp="",
            ),
            ParsedEntry(
                role="assistant",
                text="",
                content_type="completion",
                timestamp="",
            ),
            ParsedEntry(
                role="assistant",
                text="",
                content_type="completion",
                timestamp="",
            ),
        ]

        with (
            patch.object(
                monitor,
                "_resolve_active_sessions",
                return_value=[SessionInfo(session_id=session_id, file_path=jsonl_file)],
            ),
            patch.object(
                monitor,
                "_read_new_lines",
                return_value=[{"type": "assistant"}] * len(parsed_entries),
            ),
            patch(
                "codexbot.session_monitor.TranscriptParser.parse_entries"
            ) as mock_parse,
        ):
            mock_parse.return_value = (parsed_entries, {})
            messages = await monitor.check_for_updates({session_id}, bootstrap=False)

        completions = [m for m in messages if m.message_type == "completion"]
        assert len(completions) == 1
        assert completions[0].turn_id == 1

    @pytest.mark.asyncio
    async def test_turn_boundaries_keep_deterministic_order_with_completion_noise(
        self, tmp_path
    ) -> None:
        monitor = SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )
        session_id = "session-noisy-turns"
        jsonl_file = tmp_path / "session-noisy-turns.jsonl"
        jsonl_file.write_text('{"noisy": true}\n', encoding="utf-8")

        monitor.state.update_session(
            TrackedSession(
                session_id=session_id,
                file_path=str(jsonl_file),
                last_byte_offset=0,
            )
        )

        parsed_entries = [
            ParsedEntry(
                role="assistant",
                text="turn1 first",
                content_type="text",
                timestamp="",
            ),
            ParsedEntry(
                role="assistant",
                text="turn1 second",
                content_type="text",
                timestamp="",
            ),
            ParsedEntry(
                role="assistant",
                text="",
                content_type="completion",
                timestamp="",
            ),
            ParsedEntry(
                role="assistant",
                text="",
                content_type="completion",
                timestamp="",
            ),
            ParsedEntry(
                role="user",
                text="prompt for turn2",
                content_type="text",
                timestamp="",
            ),
            ParsedEntry(
                role="assistant",
                text="turn2 first",
                content_type="text",
                timestamp="",
            ),
            ParsedEntry(
                role="user",
                text="next prompt",
                content_type="text",
                timestamp="",
            ),
        ]

        with (
            patch.object(
                monitor,
                "_resolve_active_sessions",
                return_value=[SessionInfo(session_id=session_id, file_path=jsonl_file)],
            ),
            patch.object(
                monitor,
                "_read_new_lines",
                return_value=[{"type": "assistant"}] * len(parsed_entries),
            ),
            patch(
                "codexbot.session_monitor.TranscriptParser.parse_entries"
            ) as mock_parse,
        ):
            mock_parse.return_value = (parsed_entries, {})
            messages = await monitor.check_for_updates({session_id}, bootstrap=False)

        assistant_messages = [m for m in messages if m.role == "assistant"]
        assert [m.message_type for m in assistant_messages] == [
            "content",
            "content",
            "completion",
            "content",
            "completion",
        ]
        completions = [m for m in messages if m.message_type == "completion"]
        assert [m.turn_id for m in completions] == [1, 2]

        idx_turn1_last = max(
            i
            for i, m in enumerate(messages)
            if m.message_type == "content" and m.text.startswith("turn1")
        )
        idx_completion_turn1 = next(
            i
            for i, m in enumerate(messages)
            if m.message_type == "completion" and m.turn_id == 1
        )
        idx_turn2_first = next(
            i
            for i, m in enumerate(messages)
            if m.message_type == "content" and m.text.startswith("turn2")
        )
        assert idx_turn1_last < idx_completion_turn1 < idx_turn2_first


class TestSessionMonitorSessionRebinding:
    @pytest.mark.asyncio
    async def test_load_current_window_sessions_refreshes_bound_windows(
        self,
        tmp_path,
    ) -> None:
        monitor = SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )

        with patch("codexbot.session.session_manager") as mock_sm:
            mock_sm.iter_thread_bindings.return_value = [(10, 42, "@1"), (10, 43, "@2")]
            mock_sm.window_states = {"@1": object(), "@2": object()}
            mock_sm.refresh_window_session_if_stale = AsyncMock(
                side_effect=["sid-1", None]
            )

            result = await monitor._load_current_window_sessions()

        assert result == {"@1": "sid-1"}
        assert mock_sm.refresh_window_session_if_stale.await_count == 2

    @pytest.mark.asyncio
    async def test_load_current_window_sessions_includes_web_only_windows(
        self,
        tmp_path,
    ) -> None:
        """Windows created via the web transport (no thread binding) must
        still be enumerated so their transcripts feed the WebSocket bus."""
        monitor = SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )

        with patch("codexbot.session.session_manager") as mock_sm:
            # Only @1 is bound to a Telegram thread.
            mock_sm.iter_thread_bindings.return_value = [(10, 42, "@1")]
            # But @2 and @3 exist as web-only windows.
            mock_sm.window_states = {"@1": object(), "@2": object(), "@3": object()}

            async def fake_refresh(wid: str) -> str | None:
                return {"@1": "sid-1", "@2": "sid-2-web", "@3": None}[wid]

            mock_sm.refresh_window_session_if_stale = AsyncMock(
                side_effect=fake_refresh
            )

            result = await monitor._load_current_window_sessions()

        assert result == {"@1": "sid-1", "@2": "sid-2-web"}
        assert mock_sm.refresh_window_session_if_stale.await_count == 3

    @pytest.mark.asyncio
    async def test_detect_cleanup_removes_old_session_after_rebind(
        self, tmp_path
    ) -> None:
        monitor = SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )
        monitor._last_window_sessions = {"@1": "sid-old"}
        monitor.state.update_session(
            TrackedSession(
                session_id="sid-old",
                file_path=str(tmp_path / "old.jsonl"),
                last_byte_offset=0,
            )
        )

        with patch.object(
            monitor,
            "_load_current_window_sessions",
            new=AsyncMock(return_value={"@1": "sid-new"}),
        ):
            current = await monitor._detect_and_cleanup_changes()

        assert current == {"@1": "sid-new"}
        assert monitor.state.get_session("sid-old") is None
        assert monitor._last_window_sessions == {"@1": "sid-new"}

    @pytest.mark.asyncio
    async def test_new_session_reads_tail_after_bootstrap(self, tmp_path) -> None:
        monitor = SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )
        session_id = "sid-tail"
        jsonl_file = tmp_path / "sid-tail.jsonl"
        jsonl_file.write_text('{"tail": true}\n', encoding="utf-8")

        parsed_entries = [
            ParsedEntry(
                role="assistant",
                text="tail output",
                content_type="text",
                timestamp="",
            )
        ]

        with (
            patch.object(
                monitor,
                "_resolve_active_sessions",
                return_value=[SessionInfo(session_id=session_id, file_path=jsonl_file)],
            ),
            patch.object(
                monitor,
                "_read_new_lines",
                new=AsyncMock(return_value=[{"type": "assistant"}]),
            ) as mock_read,
            patch(
                "codexbot.session_monitor.TranscriptParser.parse_entries"
            ) as mock_parse,
        ):
            mock_parse.return_value = (parsed_entries, {})
            messages = await monitor.check_for_updates({session_id}, bootstrap=False)

        assert [m.text for m in messages if m.message_type == "content"] == [
            "tail output"
        ]
        mock_read.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_new_session_can_start_from_explicit_initial_offset(
        self, tmp_path, make_jsonl_entry
    ) -> None:
        monitor = SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )
        session_id = "sid-attach"
        jsonl_file = tmp_path / "sid-attach.jsonl"
        old_entry = make_jsonl_entry(msg_type="assistant", content="old backlog")
        new_entry = make_jsonl_entry(msg_type="assistant", content="new output")
        old_line = json.dumps(old_entry) + "\n"
        new_line = json.dumps(new_entry) + "\n"
        jsonl_file.write_text(old_line + new_line, encoding="utf-8")
        monitor.set_initial_offset(session_id, len(old_line.encode("utf-8")))

        with patch.object(
            monitor,
            "_resolve_active_sessions",
            return_value=[SessionInfo(session_id=session_id, file_path=jsonl_file)],
        ):
            messages = await monitor.check_for_updates({session_id}, bootstrap=False)

        assert [m.text for m in messages if m.message_type == "content"] == [
            "new output"
        ]
        tracked = monitor.state.get_session(session_id)
        assert tracked is not None
        assert tracked.last_byte_offset == jsonl_file.stat().st_size

    @pytest.mark.asyncio
    async def test_new_session_skips_history_during_bootstrap(self, tmp_path) -> None:
        monitor = SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )
        session_id = "sid-bootstrap"
        jsonl_file = tmp_path / "sid-bootstrap.jsonl"
        jsonl_file.write_text('{"bootstrap": true}\n', encoding="utf-8")

        with (
            patch.object(
                monitor,
                "_resolve_active_sessions",
                return_value=[SessionInfo(session_id=session_id, file_path=jsonl_file)],
            ),
            patch.object(monitor, "_read_new_lines", new=AsyncMock()) as mock_read,
        ):
            messages = await monitor.check_for_updates({session_id}, bootstrap=True)

        assert messages == []
        mock_read.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_tracked_session_fast_forwards_on_bootstrap(
        self, tmp_path, make_jsonl_entry
    ) -> None:
        """After restart the monitor must not replay JSONL lines that
        were written while the bot was down. The bootstrap cycle advances
        every persisted offset to the current EOF instead."""
        monitor = SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )
        session_id = "sid-restart"
        jsonl_file = tmp_path / "sid-restart.jsonl"

        pre_restart_line = (
            json.dumps(make_jsonl_entry(msg_type="assistant", content="pre-restart"))
            + "\n"
        )
        downtime_line = (
            json.dumps(
                make_jsonl_entry(
                    msg_type="assistant", content="written while bot was down"
                )
            )
            + "\n"
        )

        jsonl_file.write_text(pre_restart_line, encoding="utf-8")
        offset_at_shutdown = jsonl_file.stat().st_size
        # Simulate state.json: persisted offset matches EOF as of shutdown.
        monitor.state.update_session(
            TrackedSession(
                session_id=session_id,
                file_path=str(jsonl_file),
                last_byte_offset=offset_at_shutdown,
            )
        )

        # Agent kept writing after shutdown.
        with jsonl_file.open("a", encoding="utf-8") as f:
            f.write(downtime_line)
        eof_after_restart = jsonl_file.stat().st_size
        assert eof_after_restart > offset_at_shutdown

        with (
            patch.object(
                monitor,
                "_resolve_active_sessions",
                return_value=[SessionInfo(session_id=session_id, file_path=jsonl_file)],
            ),
            patch.object(monitor, "_read_new_lines", new=AsyncMock()) as mock_read,
        ):
            messages = await monitor.check_for_updates({session_id}, bootstrap=True)

        assert messages == []
        mock_read.assert_not_awaited()
        tracked = monitor.state.get_session(session_id)
        assert tracked is not None
        assert tracked.last_byte_offset == eof_after_restart

    @pytest.mark.asyncio
    async def test_tracked_session_delivers_new_content_after_bootstrap(
        self, tmp_path, make_jsonl_entry
    ) -> None:
        """After the bootstrap cycle, the same session must continue to
        deliver newly-written messages normally."""
        monitor = SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )
        session_id = "sid-restart-then-active"
        jsonl_file = tmp_path / "sid-restart-then-active.jsonl"
        jsonl_file.write_text(
            json.dumps(make_jsonl_entry(msg_type="assistant", content="seed")) + "\n",
            encoding="utf-8",
        )
        monitor.state.update_session(
            TrackedSession(
                session_id=session_id,
                file_path=str(jsonl_file),
                last_byte_offset=jsonl_file.stat().st_size,
            )
        )

        with patch.object(
            monitor,
            "_resolve_active_sessions",
            return_value=[SessionInfo(session_id=session_id, file_path=jsonl_file)],
        ):
            await monitor.check_for_updates({session_id}, bootstrap=True)
            # Live event after bootstrap.
            with jsonl_file.open("a", encoding="utf-8") as f:
                f.write(
                    json.dumps(make_jsonl_entry(msg_type="assistant", content="live"))
                    + "\n"
                )
            messages = await monitor.check_for_updates({session_id}, bootstrap=False)

        assert [m.text for m in messages if m.message_type == "content"] == ["live"]

    @pytest.mark.asyncio
    async def test_stop_awaits_monitor_task_cancellation(self, tmp_path) -> None:
        monitor = SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )
        monitor._running = True
        task = asyncio.create_task(asyncio.sleep(60))
        monitor._task = task

        await monitor.stop()

        assert task.done()
        assert task.cancelled()
        assert monitor._task is None
