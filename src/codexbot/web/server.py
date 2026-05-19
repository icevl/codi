"""Embed uvicorn so the web transport runs in the same asyncio loop as the bot.

`start_web_server` is intended to be called from the bot's `post_init` hook.
It:

  1. Builds the FastAPI app sharing a `bus: EventBus` with the bot.
  2. Hooks the bus into `SessionMonitor.add_listener`.
  3. Boots uvicorn in `Server.serve()` as a background asyncio.Task.

`stop_web_server` is called from `post_shutdown` to drain in-flight requests.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Optional

import uvicorn

from ..config import config
from ..session_monitor import NewMessage, SessionMonitor
from .api import create_app
from .events import EventBus, session_monitor_listener
from .streaming import stream_pane_loop

if TYPE_CHECKING:
    from telegram import Bot

logger = logging.getLogger(__name__)


Listener = Callable[[NewMessage], Awaitable[None]]
WEB_SHUTDOWN_TIMEOUT_SECONDS = 5.0


class EmbeddedUvicornServer(uvicorn.Server):
    """Uvicorn server variant for running inside the bot's event loop.

    `Server.serve()` captures process signals in the main thread. Codi already
    runs under python-telegram-bot's lifecycle, so the embedded web server must
    not install competing signal handlers.
    """

    async def serve(self, sockets: list[socket.socket] | None = None) -> None:
        await self._serve(sockets)


class WebServerHandle:
    def __init__(
        self,
        server: uvicorn.Server,
        task: asyncio.Task[None],
        bus: EventBus,
        stream_task: asyncio.Task[None] | None = None,
    ) -> None:
        self.server = server
        self.task = task
        self.bus = bus
        self.stream_task = stream_task
        self.listener: Listener | None = None


_handle: Optional[WebServerHandle] = None


async def start_web_server(
    monitor: SessionMonitor | None,
    bot: "Bot | None" = None,
) -> WebServerHandle | None:
    """Start the web UI server. Returns None when the UI is disabled.

    When `bot` is provided, web-created sessions also create/rename/delete a
    matching Telegram forum topic so the two transports stay in sync.
    """
    global _handle
    if not config.web_ui_enabled:
        logger.info("Web UI disabled (WEB_UI_PASSWORD not set)")
        return None
    if _handle is not None:
        logger.warning("Web server already running")
        return _handle

    bus = EventBus()

    if monitor is not None:

        async def _listener(msg: NewMessage) -> None:
            await session_monitor_listener(bus, msg)

        monitor.add_listener(_listener)
        listener_ref = _listener
    else:
        listener_ref = None

    app = create_app(bus, bot=bot)

    # Surface the TOTP enrollment QR + URI in the startup logs the first
    # time we generate a secret, so the operator can scan it once into
    # Google Authenticator. After that, the secret is persisted at
    # ~/.codexbot/web_ui_totp_secret and this block is skipped.
    auth = getattr(app.state, "authenticator", None)
    if (
        auth is not None
        and auth.totp_enabled
        and getattr(config, "web_ui_totp_secret_freshly_generated", False)
    ):
        uri = auth.provisioning_uri()
        ascii_qr = auth.enrollment_qr_ascii()
        logger.warning(
            "Web UI 2FA enabled — scan the QR below in Google Authenticator. "
            "Secret persists at ~/.codexbot/web_ui_totp_secret (delete it to "
            "re-enroll)."
        )
        if ascii_qr:
            for line in ascii_qr.rstrip().splitlines():
                logger.warning("%s", line)
        if uri:
            logger.warning("otpauth URI: %s", uri)
            logger.warning("Manual entry secret: %s", config.web_ui_totp_secret)

    server_config = uvicorn.Config(
        app,
        host=config.web_ui_host,
        port=config.web_ui_port,
        log_level="info",
        access_log=False,
        loop="asyncio",
        lifespan="on",
    )
    server = EmbeddedUvicornServer(server_config)

    task = asyncio.create_task(server.serve(), name="codexbot-web-server")
    stream_task = asyncio.create_task(
        stream_pane_loop(bus), name="codexbot-web-pane-stream"
    )

    logger.info(
        "Web UI listening on http://%s:%d", config.web_ui_host, config.web_ui_port
    )

    handle = WebServerHandle(server=server, task=task, bus=bus, stream_task=stream_task)
    handle.listener = listener_ref
    _handle = handle
    return handle


async def stop_web_server(monitor: SessionMonitor | None = None) -> None:
    """Gracefully shut down the embedded uvicorn server."""
    global _handle
    handle = _handle
    _handle = None
    if handle is None:
        return
    if monitor is not None and handle.listener is not None:
        monitor.remove_listener(handle.listener)
    await handle.bus.close()
    if handle.stream_task is not None:
        handle.stream_task.cancel()
        try:
            await asyncio.wait_for(
                handle.stream_task, timeout=WEB_SHUTDOWN_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            logger.warning("Pane streaming task did not stop within shutdown timeout")
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    handle.server.should_exit = True
    try:
        await asyncio.wait_for(handle.task, timeout=WEB_SHUTDOWN_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        logger.warning("Web server did not shut down within shutdown timeout")
        handle.server.force_exit = True
        handle.task.cancel()
        try:
            await asyncio.wait_for(handle.task, timeout=WEB_SHUTDOWN_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.warning("Web server task did not cancel within shutdown timeout")
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    logger.info("Web UI stopped")
