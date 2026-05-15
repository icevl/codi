"""Tests for the web transport's pub/sub bus."""

import asyncio

import pytest

from codexbot.session_monitor import NewMessage
from codexbot.web.events import EventBus


@pytest.mark.asyncio
async def test_subscribe_receives_publish() -> None:
    bus = EventBus()
    q = bus.subscribe()
    await bus.publish({"type": "ping"})
    event = await asyncio.wait_for(q.get(), timeout=0.5)
    assert event["type"] == "ping"
    assert "ts" in event


@pytest.mark.asyncio
async def test_unsubscribe_stops_delivery() -> None:
    bus = EventBus()
    q = bus.subscribe()
    bus.unsubscribe(q)
    await bus.publish({"type": "x"})
    assert q.empty()


@pytest.mark.asyncio
async def test_publish_message_serializes_new_message() -> None:
    bus = EventBus()
    q = bus.subscribe()
    msg = NewMessage(
        session_id="abc",
        text="hello",
        is_complete=True,
        message_type="content",
        content_type="text",
        role="assistant",
    )
    await bus.publish_message(msg, window_id="@7")
    event = await asyncio.wait_for(q.get(), timeout=0.5)
    assert event["type"] == "message"
    assert event["window_id"] == "@7"
    assert event["text"] == "hello"
    assert event["role"] == "assistant"
    assert event["is_complete"] is True


@pytest.mark.asyncio
async def test_publish_message_completion_type() -> None:
    bus = EventBus()
    q = bus.subscribe()
    msg = NewMessage(
        session_id="abc",
        text="",
        is_complete=True,
        message_type="completion",
        turn_id=3,
    )
    await bus.publish_message(msg, window_id="@7")
    event = await asyncio.wait_for(q.get(), timeout=0.5)
    assert event["type"] == "completion"
    assert event["turn_id"] == 3


@pytest.mark.asyncio
async def test_slow_subscriber_dropped() -> None:
    bus = EventBus(queue_size=2)
    q = bus.subscribe()
    await bus.publish({"type": "a"})
    await bus.publish({"type": "b"})
    # Third publish hits a full queue → subscriber should be dropped.
    await bus.publish({"type": "c"})
    # Still receive the two queued events.
    assert (await q.get())["type"] == "a"
    assert (await q.get())["type"] == "b"
    # And no further deliveries.
    await bus.publish({"type": "d"})
    assert q.empty()
