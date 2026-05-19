"""Tests for the embedded web server lifecycle."""

from __future__ import annotations

import pytest
import uvicorn

from codexbot.web.server import EmbeddedUvicornServer


async def _asgi_app(_scope: object, _receive: object, _send: object) -> None:
    return None


@pytest.mark.asyncio
async def test_embedded_uvicorn_server_does_not_capture_signals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = EmbeddedUvicornServer(uvicorn.Config(_asgi_app, lifespan="off"))
    calls: list[object] = []

    async def fake_serve(sockets: object | None = None) -> None:
        calls.append(sockets)

    def fail_capture_signals() -> None:
        raise AssertionError("embedded server must not capture process signals")

    monkeypatch.setattr(server, "_serve", fake_serve)
    monkeypatch.setattr(server, "capture_signals", fail_capture_signals)

    await server.serve()

    assert calls == [None]
