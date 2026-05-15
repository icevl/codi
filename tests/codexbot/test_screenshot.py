"""Tests for screenshot capture and photo media handling."""

import logging

from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import InputMediaDocument, InputMediaPhoto
from PIL import Image

from codexbot.bot import (
    SCREENSHOT_MAX_PHOTO_BYTES,
    SCREENSHOT_FONT_SIZE,
    SCREENSHOT_MAX_PHOTO_PIXELS,
    SCREENSHOT_MAX_PHOTO_DIMENSION,
    SHOT_MEDIA_DOCUMENT,
    SHOT_MEDIA_PHOTO,
    _assess_photo_payload,
    _is_photo_payload_safe,
    _prepare_screenshot_photo_preview,
    _get_screenshot_mode,
    _build_screenshot_media,
    _select_screenshot_mode,
    callback_handler,
    _set_screenshot_mode,
    screenshot_command,
)
from codexbot.handlers.callback_data import CB_KEYS_PREFIX, CB_SCREENSHOT_REFRESH
from codexbot.screenshot import (
    MAX_RENDER_COLUMNS,
    MAX_RENDER_LINES,
    StyledSegment,
    TextStyle,
    _apply_render_bounds,
)


def _make_message_update(thread_id: int = 42) -> MagicMock:
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = 1
    update.effective_chat = MagicMock()
    update.effective_chat.type = "private"
    update.message = MagicMock()
    update.message.reply_photo = AsyncMock()
    update.message.reply_document = AsyncMock()
    update.message.chat = MagicMock()
    update.message.chat.send_action = AsyncMock()
    update.message.message_thread_id = thread_id
    return update


def _make_png_bytes(width: int = 32, height: int = 10) -> bytes:
    image = BytesIO()
    Image.new("RGB", (width, height), "#111111").save(image, format="PNG")
    return image.getvalue()


def _make_callback_update(data: str, thread_id: int = 42) -> MagicMock:
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = 1
    update.effective_chat = MagicMock()
    update.effective_chat.type = "private"
    update.message = None
    update.callback_query = MagicMock()
    update.callback_query.edit_message_media = AsyncMock()
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.message = MagicMock()
    update.callback_query.message.message_thread_id = thread_id
    return update


def test_build_screenshot_media_sets_filename() -> None:
    media = _build_screenshot_media(b"abc")

    assert isinstance(media, BytesIO)
    assert media.getvalue() == b"abc"
    assert media.name == "screenshot.png"


def test_screenshot_mode_defaults_to_photo() -> None:
    assert _get_screenshot_mode({}, 42, "@5") == SHOT_MEDIA_PHOTO


@pytest.mark.asyncio
async def test_screenshot_command_replies_with_named_media() -> None:
    update = _make_message_update()
    context = MagicMock()
    context.user_data = {}

    with (
        patch("codexbot.bot.is_user_allowed", return_value=True),
        patch("codexbot.bot.session_manager") as mock_sm,
        patch("codexbot.bot.tmux_manager") as mock_tmux,
        patch("codexbot.bot._capture_screenshot_text", new_callable=AsyncMock) as mock_capture,
        patch("codexbot.bot.text_to_image", new_callable=AsyncMock) as mock_img,
    ):
        mock_sm.resolve_window_for_thread.return_value = "@5"
        mock_tmux.find_window_by_id = AsyncMock(
            return_value=MagicMock(window_id="@5")
        )
        mock_capture.return_value = "output line"
        mock_img.return_value = _make_png_bytes()

        await screenshot_command(update, context)

    reply_arg = update.message.reply_photo.call_args.kwargs["photo"]
    assert isinstance(reply_arg, BytesIO)
    assert reply_arg.name == "screenshot.png"


@pytest.mark.asyncio
async def test_send_photo_skips_fallback_on_timeout() -> None:
    """TimedOut after upload is ambiguous: skip document fallback and outer error."""
    from telegram.error import TimedOut

    update = _make_message_update()
    context = MagicMock()
    context.user_data = {}
    update.message.reply_photo.side_effect = TimedOut("read timeout")
    update.message.reply_text = AsyncMock()

    with (
        patch("codexbot.bot.is_user_allowed", return_value=True),
        patch("codexbot.bot.session_manager") as mock_sm,
        patch("codexbot.bot.tmux_manager") as mock_tmux,
        patch("codexbot.bot._capture_screenshot_text", new_callable=AsyncMock) as mock_capture,
        patch("codexbot.bot.text_to_image", new_callable=AsyncMock) as mock_img,
    ):
        mock_sm.resolve_window_for_thread.return_value = "@5"
        mock_tmux.find_window_by_id = AsyncMock(
            return_value=MagicMock(window_id="@5")
        )
        mock_capture.return_value = "output line"
        mock_img.return_value = _make_png_bytes()

        await screenshot_command(update, context)

    assert update.message.reply_photo.call_count == 1
    assert update.message.reply_document.call_count == 0
    assert update.message.reply_text.call_count == 0
    assert _get_screenshot_mode(context.user_data, 42, "@5") == SHOT_MEDIA_PHOTO


@pytest.mark.asyncio
async def test_send_photo_document_fallback_swallows_timeout() -> None:
    """When document fallback also times out, no outer error is shown."""
    from telegram.error import NetworkError

    update = _make_message_update()
    context = MagicMock()
    context.user_data = {}
    update.message.reply_photo.side_effect = RuntimeError(
        "Photo_invalid_dimensions: invalid photo size"
    )
    update.message.reply_document.side_effect = NetworkError("conn reset")
    update.message.reply_text = AsyncMock()

    with (
        patch("codexbot.bot.is_user_allowed", return_value=True),
        patch("codexbot.bot.session_manager") as mock_sm,
        patch("codexbot.bot.tmux_manager") as mock_tmux,
        patch("codexbot.bot._capture_screenshot_text", new_callable=AsyncMock) as mock_capture,
        patch("codexbot.bot.text_to_image", new_callable=AsyncMock) as mock_img,
    ):
        mock_sm.resolve_window_for_thread.return_value = "@5"
        mock_tmux.find_window_by_id = AsyncMock(
            return_value=MagicMock(window_id="@5")
        )
        mock_capture.return_value = "output line"
        mock_img.return_value = _make_png_bytes()

        await screenshot_command(update, context)

    assert update.message.reply_document.call_count == 1
    assert update.message.reply_text.call_count == 0
    assert _get_screenshot_mode(context.user_data, 42, "@5") == SHOT_MEDIA_DOCUMENT


@pytest.mark.asyncio
async def test_send_photo_fallback_to_document() -> None:
    update = _make_message_update()
    context = MagicMock()
    context.user_data = {}
    update.message.reply_photo.side_effect = RuntimeError("forced photo fail")

    with (
        patch("codexbot.bot.is_user_allowed", return_value=True),
        patch("codexbot.bot.session_manager") as mock_sm,
        patch("codexbot.bot.tmux_manager") as mock_tmux,
        patch("codexbot.bot._capture_screenshot_text", new_callable=AsyncMock) as mock_capture,
        patch("codexbot.bot.text_to_image", new_callable=AsyncMock) as mock_img,
    ):
        mock_sm.resolve_window_for_thread.return_value = "@5"
        mock_tmux.find_window_by_id = AsyncMock(
            return_value=MagicMock(window_id="@5")
        )
        mock_capture.return_value = "output line"
        mock_img.return_value = _make_png_bytes()

        await screenshot_command(update, context)

    doc_kwargs = update.message.reply_document.call_args.kwargs
    doc_arg = doc_kwargs["document"]
    assert isinstance(doc_arg, BytesIO)
    assert doc_arg.name == "screenshot.png"
    assert doc_kwargs["reply_markup"] is not None
    assert _get_screenshot_mode(context.user_data, 42, "@5") == SHOT_MEDIA_DOCUMENT


@pytest.mark.asyncio
async def test_send_photo_fallback_to_document_on_dimension_error() -> None:
    update = _make_message_update()
    context = MagicMock()
    context.user_data = {}
    update.message.reply_photo.side_effect = RuntimeError(
        "Photo_invalid_dimensions: invalid photo size"
    )

    with (
        patch("codexbot.bot.is_user_allowed", return_value=True),
        patch("codexbot.bot.session_manager") as mock_sm,
        patch("codexbot.bot.tmux_manager") as mock_tmux,
        patch("codexbot.bot._capture_screenshot_text", new_callable=AsyncMock) as mock_capture,
        patch("codexbot.bot.text_to_image", new_callable=AsyncMock) as mock_img,
    ):
        mock_sm.resolve_window_for_thread.return_value = "@5"
        mock_tmux.find_window_by_id = AsyncMock(
            return_value=MagicMock(window_id="@5")
        )
        mock_capture.return_value = "output line"
        mock_img.return_value = _make_png_bytes()

        await screenshot_command(update, context)

    assert update.message.reply_photo.call_count == 1
    assert update.message.reply_document.call_count == 1
    assert _get_screenshot_mode(context.user_data, 42, "@5") == SHOT_MEDIA_DOCUMENT


@pytest.mark.asyncio
async def test_send_photo_fallback_classifies_dimension_error_reason_in_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    update = _make_message_update()
    context = MagicMock()
    context.user_data = {}
    update.message.reply_photo.side_effect = RuntimeError(
        "Photo_invalid_dimensions: invalid photo size"
    )

    with (
        patch("codexbot.bot.is_user_allowed", return_value=True),
        patch("codexbot.bot.session_manager") as mock_sm,
        patch("codexbot.bot.tmux_manager") as mock_tmux,
        patch("codexbot.bot._capture_screenshot_text", new_callable=AsyncMock) as mock_capture,
        patch("codexbot.bot.text_to_image", new_callable=AsyncMock) as mock_img,
    ):
        mock_sm.resolve_window_for_thread.return_value = "@5"
        mock_tmux.find_window_by_id = AsyncMock(
            return_value=MagicMock(window_id="@5")
        )
        mock_capture.return_value = "output line"
        mock_img.return_value = _make_png_bytes()

        with caplog.at_level(logging.ERROR):
            await screenshot_command(update, context)

    fallback_logs = [
        rec.message
        for rec in caplog.records
        if "screenshot_send_fallback" in rec.message
    ]
    assert fallback_logs, "Expected screenshot_send_fallback log entry."
    assert any(
        "reason=Photo_invalid_dimensions" in msg
        and "attempted_mode=photo" in msg
        and "chosen_mode=document" in msg
        for msg in fallback_logs
    )
    assert _get_screenshot_mode(context.user_data, 42, "@5") == SHOT_MEDIA_DOCUMENT


@pytest.mark.asyncio
async def test_screenshot_refresh_uses_named_media() -> None:
    update = _make_callback_update(f"{CB_SCREENSHOT_REFRESH}@5")
    context = MagicMock()
    context.user_data = {}

    with (
        patch("codexbot.bot.is_user_allowed", return_value=True),
        patch("codexbot.bot.session_manager") as mock_sm,
        patch("codexbot.bot.tmux_manager") as mock_tmux,
        patch("codexbot.bot._capture_screenshot_text", new_callable=AsyncMock) as mock_capture,
        patch("codexbot.bot.text_to_image", new_callable=AsyncMock) as mock_img,
    ):
        mock_sm.is_window_bound_to_thread.return_value = True
        mock_tmux.find_window_by_id = AsyncMock(
            return_value=MagicMock(window_id="@5")
        )
        mock_capture.return_value = "pane output"
        mock_img.return_value = _make_png_bytes()

        await callback_handler(update, context)

    edit_media = update.callback_query.edit_message_media.call_args.kwargs["media"]
    assert isinstance(edit_media, InputMediaPhoto)
    assert edit_media.media.filename == "screenshot.png"
    assert _get_screenshot_mode(context.user_data, 42, "@5") == SHOT_MEDIA_PHOTO


@pytest.mark.asyncio
async def test_screenshot_refresh_checks_topic_window_match_and_clears_stale_state() -> None:
    update = _make_callback_update(f"{CB_SCREENSHOT_REFRESH}@5", thread_id=42)
    context = MagicMock()
    context.user_data = {
        "_pending_thread_id": 42,
        "_pending_thread_text": "hello",
        "some_state": "kept",
    }

    with (
        patch("codexbot.bot.is_user_allowed", return_value=True),
        patch("codexbot.bot.session_manager") as mock_sm,
    ):
        mock_sm.is_window_bound_to_thread.return_value = False
        await callback_handler(update, context)

    update.callback_query.answer.assert_awaited_with(
        "Stale screenshot callback (topic/window mismatch)", show_alert=True
    )
    assert context.user_data.get("_pending_thread_id") is None
    assert context.user_data.get("_pending_thread_text") is None


@pytest.mark.asyncio
async def test_screenshot_refresh_waits_for_thread_queue_before_edit() -> None:
    update = _make_callback_update(f"{CB_SCREENSHOT_REFRESH}@5", thread_id=42)
    context = MagicMock()
    context.user_data = {}
    events: list[str] = []
    queue = MagicMock()

    async def _join() -> None:
        events.append("join")

    async def _capture(_window_id: str) -> str:
        events.append("capture")
        return "pane output"

    async def _render(*_args: object, **_kwargs: object) -> bytes:
        events.append("render")
        return b"PNGDATA"

    async def _edit(*_args: object, **_kwargs: object) -> str:
        events.append("edit")
        return SHOT_MEDIA_PHOTO

    queue.join = AsyncMock(side_effect=_join)

    with (
        patch("codexbot.bot.is_user_allowed", return_value=True),
        patch("codexbot.bot.session_manager") as mock_sm,
        patch("codexbot.bot.tmux_manager") as mock_tmux,
        patch("codexbot.bot.get_message_queue", return_value=queue),
        patch("codexbot.bot._capture_screenshot_text", new_callable=AsyncMock, side_effect=_capture),
        patch("codexbot.bot.text_to_image", new_callable=AsyncMock, side_effect=_render),
        patch("codexbot.bot._edit_screenshot_with_fallback", new_callable=AsyncMock, side_effect=_edit),
        patch("codexbot.bot._build_screenshot_keyboard", return_value=MagicMock()),
    ):
        mock_sm.is_window_bound_to_thread.return_value = True
        mock_tmux.find_window_by_id = AsyncMock(
            return_value=MagicMock(window_id="@5")
        )

        await callback_handler(update, context)

    assert events == ["join", "capture", "render", "edit"]


@pytest.mark.asyncio
async def test_screenshot_keypress_waits_for_thread_queue_before_refresh() -> None:
    update = _make_callback_update(f"{CB_KEYS_PREFIX}ent:@5", thread_id=42)
    context = MagicMock()
    context.user_data = {}
    events: list[str] = []
    queue = MagicMock()

    async def _join() -> None:
        events.append("join")

    async def _send_keys(_wid: str, *_args: object, **_kwargs: object) -> None:
        events.append("send")

    async def _capture(_window_id: str) -> str:
        events.append("capture")
        return "pane output"

    async def _render(*_args: object, **_kwargs: object) -> bytes:
        events.append("render")
        return b"PNGDATA"

    async def _edit(*_args: object, **_kwargs: object) -> str:
        events.append("edit")
        return SHOT_MEDIA_PHOTO

    queue.join = AsyncMock(side_effect=_join)

    with (
        patch("codexbot.bot.is_user_allowed", return_value=True),
        patch("codexbot.bot.session_manager") as mock_sm,
        patch("codexbot.bot.tmux_manager") as mock_tmux,
        patch("codexbot.bot.get_message_queue", return_value=queue),
        patch("codexbot.bot._capture_screenshot_text", new_callable=AsyncMock, side_effect=_capture),
        patch("codexbot.bot.text_to_image", new_callable=AsyncMock, side_effect=_render),
        patch("codexbot.bot._edit_screenshot_with_fallback", new_callable=AsyncMock, side_effect=_edit),
        patch("codexbot.bot._build_screenshot_keyboard", return_value=MagicMock()),
        patch("codexbot.bot.asyncio.sleep", new=AsyncMock()),
    ):
        mock_sm.is_window_bound_to_thread.return_value = True
        mock_tmux.find_window_by_id = AsyncMock(
            return_value=MagicMock(window_id="@5")
        )
        mock_tmux.send_keys = AsyncMock(side_effect=_send_keys)

        await callback_handler(update, context)

    assert events[0] == "join"
    assert "capture" in events
    assert events.index("join") < events.index("capture")


@pytest.mark.asyncio
async def test_screenshot_refresh_switches_mode_on_photo_edit_failure() -> None:
    update = _make_callback_update(f"{CB_SCREENSHOT_REFRESH}@5")
    context = MagicMock()
    context.user_data = {}
    _set_screenshot_mode(context.user_data, 42, "@5", SHOT_MEDIA_PHOTO)
    update.callback_query.edit_message_media.side_effect = [RuntimeError("photo fail"), None]

    with (
        patch("codexbot.bot.is_user_allowed", return_value=True),
        patch("codexbot.bot.session_manager") as mock_sm,
        patch("codexbot.bot.tmux_manager") as mock_tmux,
        patch("codexbot.bot._capture_screenshot_text", new_callable=AsyncMock) as mock_capture,
        patch("codexbot.bot.text_to_image", new_callable=AsyncMock) as mock_img,
    ):
        mock_sm.is_window_bound_to_thread.return_value = True
        mock_tmux.find_window_by_id = AsyncMock(
            return_value=MagicMock(window_id="@5")
        )
        mock_capture.return_value = "pane output"
        mock_img.return_value = _make_png_bytes()

        await callback_handler(update, context)

    assert update.callback_query.edit_message_media.call_count == 2
    first_media = update.callback_query.edit_message_media.call_args_list[0].kwargs[
        "media"
    ]
    second_media = update.callback_query.edit_message_media.call_args_list[1].kwargs[
        "media"
    ]
    assert isinstance(first_media, InputMediaPhoto)
    assert isinstance(second_media, InputMediaDocument)
    assert _get_screenshot_mode(context.user_data, 42, "@5") == SHOT_MEDIA_DOCUMENT


@pytest.mark.asyncio
async def test_screenshot_refresh_falls_back_to_document_on_dimension_error() -> None:
    update = _make_callback_update(f"{CB_SCREENSHOT_REFRESH}@5")
    context = MagicMock()
    context.user_data = {}
    _set_screenshot_mode(context.user_data, 42, "@5", SHOT_MEDIA_PHOTO)
    update.callback_query.edit_message_media.side_effect = [
        RuntimeError("Photo_invalid_dimensions: invalid photo"),
        None,
    ]

    with (
        patch("codexbot.bot.is_user_allowed", return_value=True),
        patch("codexbot.bot.session_manager") as mock_sm,
        patch("codexbot.bot.tmux_manager") as mock_tmux,
        patch("codexbot.bot._capture_screenshot_text", new_callable=AsyncMock) as mock_capture,
        patch("codexbot.bot.text_to_image", new_callable=AsyncMock) as mock_img,
    ):
        mock_sm.is_window_bound_to_thread.return_value = True
        mock_tmux.find_window_by_id = AsyncMock(
            return_value=MagicMock(window_id="@5")
        )
        mock_capture.return_value = "pane output"
        mock_img.return_value = _make_png_bytes()

        await callback_handler(update, context)

    assert update.callback_query.edit_message_media.call_count == 2
    first_media = update.callback_query.edit_message_media.call_args_list[0].kwargs[
        "media"
    ]
    second_media = update.callback_query.edit_message_media.call_args_list[1].kwargs[
        "media"
    ]
    assert isinstance(first_media, InputMediaPhoto)
    assert isinstance(second_media, InputMediaDocument)
    assert _get_screenshot_mode(context.user_data, 42, "@5") == SHOT_MEDIA_DOCUMENT


@pytest.mark.asyncio
async def test_screenshot_refresh_skips_mode_fallback_on_timeout() -> None:
    """TimedOut on edit is ambiguous — don't try the alternate mode."""
    from telegram.error import TimedOut

    update = _make_callback_update(f"{CB_SCREENSHOT_REFRESH}@5")
    context = MagicMock()
    context.user_data = {}
    _set_screenshot_mode(context.user_data, 42, "@5", SHOT_MEDIA_PHOTO)
    update.callback_query.edit_message_media.side_effect = TimedOut("ack timeout")

    with (
        patch("codexbot.bot.is_user_allowed", return_value=True),
        patch("codexbot.bot.session_manager") as mock_sm,
        patch("codexbot.bot.tmux_manager") as mock_tmux,
        patch("codexbot.bot._capture_screenshot_text", new_callable=AsyncMock) as mock_capture,
        patch("codexbot.bot.text_to_image", new_callable=AsyncMock) as mock_img,
    ):
        mock_sm.is_window_bound_to_thread.return_value = True
        mock_tmux.find_window_by_id = AsyncMock(
            return_value=MagicMock(window_id="@5")
        )
        mock_capture.return_value = "pane output"
        mock_img.return_value = _make_png_bytes()

        await callback_handler(update, context)

    assert update.callback_query.edit_message_media.call_count == 1
    assert _get_screenshot_mode(context.user_data, 42, "@5") == SHOT_MEDIA_PHOTO


@pytest.mark.asyncio
async def test_screenshot_refresh_continues_in_document_mode() -> None:
    update = _make_callback_update(f"{CB_SCREENSHOT_REFRESH}@5")
    context = MagicMock()
    context.user_data = {}
    _set_screenshot_mode(context.user_data, 42, "@5", SHOT_MEDIA_DOCUMENT)

    with (
        patch("codexbot.bot.is_user_allowed", return_value=True),
        patch("codexbot.bot.session_manager") as mock_sm,
        patch("codexbot.bot.tmux_manager") as mock_tmux,
        patch("codexbot.bot._capture_screenshot_text", new_callable=AsyncMock) as mock_capture,
        patch("codexbot.bot.text_to_image", new_callable=AsyncMock) as mock_img,
    ):
        mock_sm.is_window_bound_to_thread.return_value = True
        mock_tmux.find_window_by_id = AsyncMock(
            return_value=MagicMock(window_id="@5")
        )
        mock_capture.return_value = "pane output"
        mock_img.return_value = _make_png_bytes()

        await callback_handler(update, context)

    edit_media = update.callback_query.edit_message_media.call_args.kwargs["media"]
    assert isinstance(edit_media, InputMediaDocument)
    assert _get_screenshot_mode(context.user_data, 42, "@5") == SHOT_MEDIA_DOCUMENT


def test_screenshot_font_size_is_increased() -> None:
    assert SCREENSHOT_FONT_SIZE >= 40


def test_screenshot_text_to_image_has_size_guard() -> None:
    raw_lines = [
        [StyledSegment("x" * (MAX_RENDER_COLUMNS + 40), TextStyle(), 0)]
        for _ in range(MAX_RENDER_LINES + 30)
    ]
    bounded_lines, _ = _apply_render_bounds(raw_lines)

    assert len(bounded_lines) == MAX_RENDER_LINES
    assert all(
        sum(len(seg.text) for seg in line) <= MAX_RENDER_COLUMNS
        for line in bounded_lines
    )

    oversized_png = b"x" * (SCREENSHOT_MAX_PHOTO_BYTES + 1)
    assert _select_screenshot_mode({}, 42, "@5", oversized_png) == SHOT_MEDIA_PHOTO


def test_prepare_screenshot_photo_preview_resizes_large_image() -> None:
    oversized_png = _make_png_bytes(4000, 3000)
    preview_bytes, metadata = _prepare_screenshot_photo_preview(oversized_png)
    preview_safety = _assess_photo_payload(preview_bytes)

    assert metadata["resized"] is True
    assert metadata["reason"] == "resized_preview"
    assert preview_safety["is_safe"] is True


@pytest.mark.asyncio
async def test_screenshot_command_resizes_oversized_preview_to_photo() -> None:
    update = _make_message_update()
    context = MagicMock()
    context.user_data = {}

    with (
        patch("codexbot.bot.is_user_allowed", return_value=True),
        patch("codexbot.bot.session_manager") as mock_sm,
        patch("codexbot.bot.tmux_manager") as mock_tmux,
        patch("codexbot.bot._capture_screenshot_text", new_callable=AsyncMock) as mock_capture,
        patch("codexbot.bot.text_to_image", new_callable=AsyncMock) as mock_img,
    ):
        mock_sm.resolve_window_for_thread.return_value = "@5"
        mock_tmux.find_window_by_id = AsyncMock(
            return_value=MagicMock(window_id="@5")
        )
        mock_capture.return_value = "output line"
        mock_img.return_value = _make_png_bytes(4000, 3000)

        await screenshot_command(update, context)

    update.message.reply_photo.assert_awaited_once()
    update.message.reply_document.assert_not_awaited()
    sent_photo = update.message.reply_photo.call_args.kwargs["photo"]
    sent_safety = _assess_photo_payload(sent_photo.getvalue())
    assert sent_safety["is_safe"] is True


def test_is_photo_payload_safe_checks_dimensions() -> None:
    small = BytesIO()
    Image.new("RGB", (100, 40), "#111111").save(small, format="PNG")
    assert _is_photo_payload_safe(small.getvalue()) is True

    wide = BytesIO()
    Image.new("RGB", (SCREENSHOT_MAX_PHOTO_DIMENSION + 1, 100), "#111111").save(
        wide,
        format="PNG",
    )
    assert _is_photo_payload_safe(wide.getvalue()) is False


def test_is_photo_payload_rejects_large_canvas() -> None:
    oversized_area = BytesIO()
    Image.new("RGB", (4000, 3000), "#111111").save(oversized_area, format="PNG")
    assert 4000 * 3000 > SCREENSHOT_MAX_PHOTO_PIXELS
    assert _is_photo_payload_safe(oversized_area.getvalue()) is False


def test_is_photo_payload_falls_back_on_invalid_image() -> None:
    assert _is_photo_payload_safe(b"not-an-image") is False


def test_assess_photo_payload_reports_reasons() -> None:
    oversized_bytes = b"".join([b"x"] * (SCREENSHOT_MAX_PHOTO_BYTES + 1))
    result = _assess_photo_payload(oversized_bytes)
    assert result["is_safe"] is False
    assert result["reason"] == "exceeds_max_bytes"

    over_dimension = BytesIO()
    Image.new(
        "RGB",
        (SCREENSHOT_MAX_PHOTO_DIMENSION + 1, 1),
        "#111111",
    ).save(over_dimension, format="PNG")
    result = _assess_photo_payload(over_dimension.getvalue())
    assert result["is_safe"] is False
    assert result["reason"] == "exceeds_max_dimension"
    assert result["width"] == SCREENSHOT_MAX_PHOTO_DIMENSION + 1
    assert result["height"] == 1
