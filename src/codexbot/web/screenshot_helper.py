"""Pane → PNG capture for the web transport.

Shares the rendering pipeline used by the Telegram bot but with a font size
tuned for inline browser display rather than Telegram's photo preview.
"""

from __future__ import annotations

import logging

from ..screenshot import text_to_image
from ..tmux_manager import tmux_manager

logger = logging.getLogger(__name__)

WEB_SCREENSHOT_FONT_SIZE = 28


async def capture_screenshot(window_id: str) -> bytes | None:
    """Capture the active pane of `window_id` and return PNG bytes."""
    text = await tmux_manager.capture_pane(window_id, with_ansi=True)
    if text is None:
        logger.warning("No pane content captured for window %s", window_id)
        return None
    try:
        return await text_to_image(
            text, font_size=WEB_SCREENSHOT_FONT_SIZE, with_ansi=True
        )
    except Exception as exc:
        logger.error("Screenshot rendering failed for %s: %s", window_id, exc)
        return None
