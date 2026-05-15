"""Tests for completion ordering and status behavior in message queue worker."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import RetryAfter

from codexbot.handlers import message_queue as mq
from codexbot.handlers.message_queue import (
    enqueue_completion_message,
    enqueue_content_message,
    enqueue_status_update,
    get_or_create_queue,
)


class TestMessageQueueCompletionOrdering:
    @pytest.mark.asyncio
    async def test_completion_task_is_not_merged_and_runs_after_content(self) -> None:
        await mq.shutdown_workers()
        mq._tool_msg_ids.clear()
        mq._status_msg_info.clear()
        mq._flood_until.clear()
        mq._completion_pending_turns.clear()
        mq._completion_complete_turns.clear()

        bot = AsyncMock()
        queue = get_or_create_queue(bot, user_id=1)

        with patch(
            "codexbot.handlers.message_queue._process_content_task",
            new_callable=AsyncMock,
        ) as mock_process:
            await enqueue_content_message(
                bot=bot,
                user_id=1,
                window_id="@5",
                parts=["first output"],
                content_type="text",
            )
            await enqueue_content_message(
                bot=bot,
                user_id=1,
                window_id="@5",
                parts=["second output"],
                content_type="text",
            )
            await enqueue_completion_message(
                bot=bot,
                user_id=1,
                window_id="@5",
                session_id="s1",
                turn_id=42,
                completion_text="✅ done",
            )

            await queue.join()

        assert mock_process.await_count == 2
        first_task = mock_process.await_args_list[0].args[2]
        second_task = mock_process.await_args_list[1].args[2]

        assert first_task.task_type == "content"
        assert first_task.parts == ["first output", "second output"]
        assert second_task.task_type == "completion"
        assert second_task.parts == ["✅ done"]

        await mq.shutdown_workers()

    @pytest.mark.asyncio
    async def test_completion_keeps_final_status_refresh_sequence(self) -> None:
        await mq.shutdown_workers()
        mq._tool_msg_ids.clear()
        mq._status_msg_info.clear()
        mq._flood_until.clear()
        mq._completion_pending_turns.clear()
        mq._completion_complete_turns.clear()

        bot = AsyncMock()
        queue = get_or_create_queue(bot, user_id=2)
        events: list[str] = []

        async def _fake_send_with_fallback(
            _bot: AsyncMock,
            _chat_id: int,
            text: str,
            **_kwargs: object,
        ) -> MagicMock:
            events.append(f"send:{text}")
            sent = MagicMock()
            sent.message_id = len(events)
            return sent

        async def _fake_check_and_send_status(
            _bot: AsyncMock,
            _user_id: int,
            _window_id: str,
            _thread_id: int | None,
        ) -> None:
            events.append("status")

        with (
            patch(
                "codexbot.handlers.message_queue.session_manager.resolve_chat_id",
                return_value=100,
            ),
            patch(
                "codexbot.handlers.message_queue.send_with_fallback",
                side_effect=_fake_send_with_fallback,
            ),
            patch(
                "codexbot.handlers.message_queue._check_and_send_status",
                side_effect=_fake_check_and_send_status,
            ),
            patch(
                "codexbot.handlers.message_queue._send_task_images",
                new_callable=AsyncMock,
            ),
            patch(
                "codexbot.handlers.message_queue._convert_status_to_content",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            await enqueue_content_message(
                bot=bot,
                user_id=2,
                window_id="@9",
                parts=["final output"],
                content_type="text",
            )
            await enqueue_completion_message(
                bot=bot,
                user_id=2,
                window_id="@9",
                completion_text="✅ done",
            )

            await queue.join()

        assert events == [
            "send:final output",
            "status",
            "send:✅ done",
            "status",
        ]

        await mq.shutdown_workers()

    @pytest.mark.asyncio
    async def test_completion_duplicates_are_filtered_by_session_turn(self) -> None:
        await mq.shutdown_workers()
        mq._tool_msg_ids.clear()
        mq._status_msg_info.clear()
        mq._flood_until.clear()
        mq._completion_pending_turns.clear()
        mq._completion_complete_turns.clear()

        bot = AsyncMock()
        queue = get_or_create_queue(bot, user_id=5)

        with patch(
            "codexbot.handlers.message_queue._process_content_task",
            new_callable=AsyncMock,
        ) as mock_process:
            await enqueue_completion_message(
                bot=bot,
                user_id=5,
                window_id="@5",
                session_id="s1",
                turn_id=11,
                completion_text="✅ done",
            )
            await enqueue_completion_message(
                bot=bot,
                user_id=5,
                window_id="@5",
                session_id="s1",
                turn_id=11,
                completion_text="✅ done",
            )

            await queue.join()

        assert mock_process.await_count == 1
        task = mock_process.await_args.args[2]
        assert task.task_type == "completion"

        await mq.shutdown_workers()

    @pytest.mark.asyncio
    async def test_completion_diagnostic_events_track_finalization(self) -> None:
        await mq.shutdown_workers()
        mq._tool_msg_ids.clear()
        mq._status_msg_info.clear()
        mq._flood_until.clear()
        mq._completion_pending_turns.clear()
        mq._completion_complete_turns.clear()
        mq._completion_diagnostic_events.clear()

        bot = AsyncMock()
        queue = get_or_create_queue(bot, user_id=7)

        with patch(
            "codexbot.handlers.message_queue._process_content_task",
            new_callable=AsyncMock,
        ):
            await enqueue_completion_message(
                bot=bot,
                user_id=7,
                window_id="@5",
                session_id="s1",
                turn_id=13,
                completion_text="✅ done",
            )
            await queue.join()

        events = mq.get_diagnostic_events(7, thread_id=None)
        event_names = [event["event"] for event in events]
        assert event_names == [
            "accepted",
            "claimed",
            "sent",
            "finalized",
        ]
        assert events[-1]["reason"] == "completion_complete"

        await mq.shutdown_workers()

    @pytest.mark.asyncio
    async def test_completion_diagnostic_events_record_release_and_error(self) -> None:
        await mq.shutdown_workers()
        mq._tool_msg_ids.clear()
        mq._status_msg_info.clear()
        mq._flood_until.clear()
        mq._completion_pending_turns.clear()
        mq._completion_complete_turns.clear()
        mq._completion_diagnostic_events.clear()

        bot = AsyncMock()
        queue = get_or_create_queue(bot, user_id=8)
        attempts = {"count": 0}

        async def _failing_send(
            _bot: AsyncMock,
            _user_id: int,
            _task: mq.MessageTask,
        ) -> None:
            attempts["count"] += 1
            raise TimeoutError("transport failed")

        with (
            patch(
                "codexbot.handlers.message_queue._process_content_task",
                new_callable=AsyncMock,
                side_effect=_failing_send,
            ),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            await enqueue_completion_message(
                bot=bot,
                user_id=8,
                window_id="@6",
                session_id="s2",
                turn_id=14,
                completion_text="✅ done",
            )
            await queue.join()

        assert attempts["count"] == mq.COMPLETION_RETRY_ATTEMPTS
        events = mq.get_diagnostic_events(8, thread_id=None)
        event_names = [event["event"] for event in events]
        assert event_names == [
            "accepted",
            "claimed",
            "retrying",
            "retrying",
            "release",
            "error",
        ]
        assert mq._completion_pending_turns.get((8, 0), {}) == {}

        await mq.shutdown_workers()

    @pytest.mark.asyncio
    async def test_completion_diagnostic_history_is_capped(self) -> None:
        await mq.shutdown_workers()
        mq._tool_msg_ids.clear()
        mq._status_msg_info.clear()
        mq._flood_until.clear()
        mq._completion_pending_turns.clear()
        mq._completion_complete_turns.clear()
        mq._completion_diagnostic_events.clear()

        bot = AsyncMock()
        queue = get_or_create_queue(bot, user_id=9)

        with patch(
            "codexbot.handlers.message_queue._process_content_task",
            new_callable=AsyncMock,
        ):
            for turn_id in range(30):
                await enqueue_completion_message(
                    bot=bot,
                    user_id=9,
                    window_id="@5",
                    session_id="s3",
                    turn_id=turn_id,
                    completion_text=f"✅ done {turn_id}",
                )
            await queue.join()

        events = mq.get_diagnostic_events(9, thread_id=None)
        assert len(events) == mq.COMPLETION_DIAGNOSTIC_EVENT_MAXLEN
        assert events[-1]["turn_id"] == 29

        await mq.shutdown_workers()

    @pytest.mark.asyncio
    async def test_completion_retry_reorders_after_content_and_preserves_order(self) -> None:
        await mq.shutdown_workers()
        mq._tool_msg_ids.clear()
        mq._status_msg_info.clear()
        mq._flood_until.clear()
        mq._completion_pending_turns.clear()
        mq._completion_complete_turns.clear()

        bot = AsyncMock()
        queue = get_or_create_queue(bot, user_id=6)
        attempts: dict[str, int] = {"completion": 0}
        order: list[str] = []

        async def _fake_process_content_task(
            _bot: AsyncMock, _user_id: int, task: mq.MessageTask
        ) -> None:
            order.append(task.task_type)
            if task.task_type == "completion":
                attempts["completion"] += 1
                if attempts["completion"] == 1:
                    raise TimeoutError("retry transport")

            return None

        with patch(
            "codexbot.handlers.message_queue._process_content_task",
            new_callable=AsyncMock,
            side_effect=_fake_process_content_task,
        ):
            await enqueue_content_message(
                bot=bot,
                user_id=6,
                window_id="@5",
                parts=["first output"],
                content_type="text",
            )
            await enqueue_completion_message(
                bot=bot,
                user_id=6,
                window_id="@5",
                session_id="s1",
                turn_id=3,
                completion_text="✅ done",
            )
            await enqueue_content_message(
                bot=bot,
                user_id=6,
                window_id="@5",
                parts=["after completion"],
                content_type="text",
            )

            await queue.join()

        assert order == [
            "content",
            "completion",
            "completion",
            "content",
        ]
        assert attempts["completion"] == 2

        await mq.shutdown_workers()

    @pytest.mark.asyncio
    async def test_completion_retry_honors_send_failure_without_success(self) -> None:
        await mq.shutdown_workers()
        mq._tool_msg_ids.clear()
        mq._status_msg_info.clear()
        mq._flood_until.clear()
        mq._completion_pending_turns.clear()
        mq._completion_complete_turns.clear()

        bot = AsyncMock()
        queue = get_or_create_queue(bot, user_id=7)
        attempts: dict[str, int] = {"completion": 0}

        async def _fake_send_with_fallback(
            _bot: AsyncMock,
            _chat_id: int,
            _text: str,
            **_kwargs: object,
        ) -> None:
            attempts["completion"] += 1
            return None

        with (
            patch(
                "codexbot.handlers.message_queue.send_with_fallback",
                side_effect=_fake_send_with_fallback,
            ),
            patch(
                "codexbot.handlers.message_queue._send_task_images",
                new_callable=AsyncMock,
            ),
            patch(
                "codexbot.handlers.message_queue._convert_status_to_content",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("codexbot.handlers.message_queue._check_and_send_status", new_callable=AsyncMock),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            await enqueue_completion_message(
                bot=bot,
                user_id=7,
                window_id="@5",
                session_id="s1",
                turn_id=10,
                completion_text="✅ done",
            )

            await queue.join()

        assert attempts["completion"] == 3

        await mq.shutdown_workers()

    @pytest.mark.asyncio
    async def test_completion_retry_does_not_duplicate_after_post_send_error(self) -> None:
        await mq.shutdown_workers()
        mq._tool_msg_ids.clear()
        mq._status_msg_info.clear()
        mq._flood_until.clear()
        mq._completion_pending_turns.clear()
        mq._completion_complete_turns.clear()

        bot = AsyncMock()
        queue = get_or_create_queue(bot, user_id=8)
        sent_parts: list[str] = []
        status_calls = 0

        async def _fake_send_with_fallback(
            _bot: AsyncMock,
            _chat_id: int,
            text: str,
            **_kwargs: object,
        ) -> MagicMock:
            sent_parts.append(text)
            sent = MagicMock()
            sent.message_id = len(sent_parts)
            return sent

        async def _fake_check_and_send_status(
            _bot: AsyncMock,
            _user_id: int,
            _window_id: str,
            _thread_id: int | None,
        ) -> None:
            nonlocal status_calls
            status_calls += 1
            if status_calls == 2:
                raise RetryAfter(1)

        with (
            patch(
                "codexbot.handlers.message_queue.session_manager.resolve_chat_id",
                return_value=100,
            ),
            patch(
                "codexbot.handlers.message_queue.send_with_fallback",
                side_effect=_fake_send_with_fallback,
            ),
            patch(
                "codexbot.handlers.message_queue._check_and_send_status",
                side_effect=_fake_check_and_send_status,
            ),
            patch(
                "codexbot.handlers.message_queue._send_task_images",
                new_callable=AsyncMock,
            ),
            patch(
                "codexbot.handlers.message_queue._convert_status_to_content",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            await enqueue_content_message(
                bot=bot,
                user_id=8,
                window_id="@5",
                parts=["before completion"],
                content_type="text",
            )
            await enqueue_completion_message(
                bot=bot,
                user_id=8,
                window_id="@5",
                session_id="s1",
                turn_id=12,
                completion_text="✅ done",
            )
            await enqueue_content_message(
                bot=bot,
                user_id=8,
                window_id="@5",
                parts=["after completion"],
                content_type="text",
            )

            await queue.join()

        assert sent_parts == [
            "before completion",
            "✅ done",
            "after completion",
        ]
        assert mq._completion_complete_turns[(8, 0)]["s1"] == {12}

        await mq.shutdown_workers()

    @pytest.mark.asyncio
    async def test_thread_queues_are_isolated_and_do_not_merge_across_threads(self) -> None:
        await mq.shutdown_workers()
        mq._tool_msg_ids.clear()
        mq._status_msg_info.clear()
        mq._flood_until.clear()
        mq._completion_pending_turns.clear()
        mq._completion_complete_turns.clear()

        bot = AsyncMock()
        queue_10 = get_or_create_queue(bot, user_id=3, thread_id=10)
        queue_20 = get_or_create_queue(bot, user_id=3, thread_id=20)

        with patch(
            "codexbot.handlers.message_queue._process_content_task",
            new_callable=AsyncMock,
        ) as mock_process:
            await enqueue_content_message(
                bot=bot,
                user_id=3,
                window_id="@5",
                parts=["thread10 output"],
                content_type="text",
                thread_id=10,
            )
            await enqueue_content_message(
                bot=bot,
                user_id=3,
                window_id="@5",
                parts=["thread20 output"],
                content_type="text",
                thread_id=20,
            )

            await queue_10.join()
            await queue_20.join()

        tasks = [call.args[2] for call in mock_process.await_args_list]
        assert len(tasks) == 2
        assert {task.thread_id for task in tasks} == {10, 20}
        assert sorted([tuple(task.parts) for task in tasks]) == [
            ("thread10 output",),
            ("thread20 output",),
        ]

        await mq.shutdown_workers()

    @pytest.mark.asyncio
    async def test_queue_load_keeps_content_completion_order_and_dedupes_turns(self) -> None:
        await mq.shutdown_workers()
        mq._tool_msg_ids.clear()
        mq._status_msg_info.clear()
        mq._flood_until.clear()
        mq._completion_pending_turns.clear()
        mq._completion_complete_turns.clear()

        bot = AsyncMock()
        queue = get_or_create_queue(bot, user_id=10)
        observed: list[tuple[str, int | None, list[str]]] = []

        async def _fake_process_content_task(
            _bot: AsyncMock, _user_id: int, task: mq.MessageTask
        ) -> None:
            observed.append((task.task_type, task.turn_id, list(task.parts)))

        with patch(
            "codexbot.handlers.message_queue._process_content_task",
            new_callable=AsyncMock,
            side_effect=_fake_process_content_task,
        ):
            await enqueue_content_message(
                bot=bot,
                user_id=10,
                window_id="@5",
                parts=["turn1 chunk1"],
                content_type="text",
            )
            await enqueue_content_message(
                bot=bot,
                user_id=10,
                window_id="@5",
                parts=["turn1 chunk2"],
                content_type="text",
            )
            await enqueue_completion_message(
                bot=bot,
                user_id=10,
                window_id="@5",
                session_id="s1",
                turn_id=1,
                completion_text="✅ turn1 done",
            )
            await enqueue_completion_message(
                bot=bot,
                user_id=10,
                window_id="@5",
                session_id="s1",
                turn_id=1,
                completion_text="✅ turn1 done",
            )
            await enqueue_content_message(
                bot=bot,
                user_id=10,
                window_id="@5",
                parts=["turn2 chunk1"],
                content_type="text",
            )
            await enqueue_completion_message(
                bot=bot,
                user_id=10,
                window_id="@5",
                session_id="s1",
                turn_id=2,
                completion_text="✅ turn2 done",
            )

            await queue.join()

        assert [task_type for task_type, _, _ in observed] == [
            "content",
            "completion",
            "content",
            "completion",
        ]
        assert observed[0][2] == ["turn1 chunk1", "turn1 chunk2"]
        assert [turn_id for task_type, turn_id, _ in observed if task_type == "completion"] == [
            1,
            2,
        ]
        assert mq._completion_complete_turns[(10, 0)]["s1"] == {1, 2}

        await mq.shutdown_workers()

    @pytest.mark.asyncio
    async def test_flood_control_is_thread_scoped(self) -> None:
        await mq.shutdown_workers()
        mq._tool_msg_ids.clear()
        mq._status_msg_info.clear()
        mq._flood_until.clear()
        mq._completion_pending_turns.clear()
        mq._completion_complete_turns.clear()

        bot = AsyncMock()
        queue_10 = get_or_create_queue(bot, user_id=4, thread_id=10)
        queue_20 = get_or_create_queue(bot, user_id=4, thread_id=20)
        mq._flood_until[(4, 10)] = time.monotonic() + 9999

        with patch(
            "codexbot.handlers.message_queue._process_status_update_task",
            new_callable=AsyncMock,
        ) as mock_status:
            await enqueue_status_update(
                bot=bot,
                user_id=4,
                window_id="@5",
                status_text="thread10 status",
                thread_id=10,
            )
            await enqueue_status_update(
                bot=bot,
                user_id=4,
                window_id="@5",
                status_text="thread20 status",
                thread_id=20,
            )

            await queue_10.join()
            await queue_20.join()

        assert mock_status.await_count == 1
        processed = mock_status.await_args_list[0].args[2]
        assert processed.thread_id == 20

        await mq.shutdown_workers()

    @pytest.mark.asyncio
    async def test_content_with_convert_status_disabled_sends_new_message(self) -> None:
        await mq.shutdown_workers()
        mq._tool_msg_ids.clear()
        mq._status_msg_info.clear()
        mq._flood_until.clear()
        mq._completion_pending_turns.clear()
        mq._completion_complete_turns.clear()

        bot = AsyncMock()
        queue = get_or_create_queue(bot, user_id=11)
        sent_parts: list[str] = []

        async def _fake_send_with_fallback(
            _bot: AsyncMock,
            _chat_id: int,
            text: str,
            **_kwargs: object,
        ) -> MagicMock:
            sent_parts.append(text)
            sent = MagicMock()
            sent.message_id = 1
            return sent

        with (
            patch(
                "codexbot.handlers.message_queue.session_manager.resolve_chat_id",
                return_value=100,
            ),
            patch(
                "codexbot.handlers.message_queue.send_with_fallback",
                side_effect=_fake_send_with_fallback,
            ),
            patch(
                "codexbot.handlers.message_queue._convert_status_to_content",
                new_callable=AsyncMock,
                return_value=123,
            ) as mock_convert,
            patch(
                "codexbot.handlers.message_queue._check_and_send_status",
                new_callable=AsyncMock,
            ),
            patch(
                "codexbot.handlers.message_queue._send_task_images",
                new_callable=AsyncMock,
            ),
        ):
            await enqueue_content_message(
                bot=bot,
                user_id=11,
                window_id="@5",
                parts=["final output"],
                content_type="text",
                convert_status_to_content=False,
            )
            await queue.join()

        mock_convert.assert_not_awaited()
        assert sent_parts == ["final output"]

        await mq.shutdown_workers()

    @pytest.mark.asyncio
    async def test_completion_messages_never_convert_status_to_content(self) -> None:
        await mq.shutdown_workers()
        mq._tool_msg_ids.clear()
        mq._status_msg_info.clear()
        mq._flood_until.clear()
        mq._completion_pending_turns.clear()
        mq._completion_complete_turns.clear()

        bot = AsyncMock()
        queue = get_or_create_queue(bot, user_id=12)
        sent_parts: list[str] = []

        async def _fake_send_with_fallback(
            _bot: AsyncMock,
            _chat_id: int,
            text: str,
            **_kwargs: object,
        ) -> MagicMock:
            sent_parts.append(text)
            sent = MagicMock()
            sent.message_id = 1
            return sent

        with (
            patch(
                "codexbot.handlers.message_queue.session_manager.resolve_chat_id",
                return_value=100,
            ),
            patch(
                "codexbot.handlers.message_queue.send_with_fallback",
                side_effect=_fake_send_with_fallback,
            ),
            patch(
                "codexbot.handlers.message_queue._convert_status_to_content",
                new_callable=AsyncMock,
                return_value=123,
            ) as mock_convert,
            patch(
                "codexbot.handlers.message_queue._check_and_send_status",
                new_callable=AsyncMock,
            ),
            patch(
                "codexbot.handlers.message_queue._send_task_images",
                new_callable=AsyncMock,
            ),
        ):
            await enqueue_completion_message(
                bot=bot,
                user_id=12,
                window_id="@7",
                completion_text="✅ done",
                session_id="s1",
                turn_id=1,
            )
            await queue.join()

        mock_convert.assert_not_awaited()
        assert sent_parts == ["✅ done"]

        await mq.shutdown_workers()
