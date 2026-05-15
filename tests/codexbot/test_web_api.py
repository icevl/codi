"""Smoke tests for the FastAPI web transport.

These exercise the HTTP plumbing — auth gates, request bodies, response
shapes — by stubbing the global `tmux_manager` and `session_manager`
singletons. The backend dependencies (libtmux, transcript files) are
not exercised here.
"""

from __future__ import annotations

import json
from typing import Any

import pyotp
import pytest
from fastapi.testclient import TestClient

from codexbot import config as config_module
from codexbot.tmux_manager import TmuxWindow
from codexbot.web.api import create_app
from codexbot.web.events import EventBus


def _baseline_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the web UI knobs to test-friendly defaults."""
    monkeypatch.setattr(
        config_module.config, "web_ui_password", "hunter2", raising=False
    )
    monkeypatch.setattr(
        config_module.config, "web_ui_secret", "test-secret", raising=False
    )
    monkeypatch.setattr(config_module.config, "web_ui_enabled", True, raising=False)
    # Tests default to no 2FA; override with the `totp_secret` fixture.
    monkeypatch.setattr(
        config_module.config, "web_ui_totp_required", False, raising=False
    )
    monkeypatch.setattr(
        config_module.config, "web_ui_totp_secret", "", raising=False
    )
    monkeypatch.setattr(
        config_module.config, "web_ui_totp_issuer", "CodexBot-Test", raising=False
    )
    monkeypatch.setattr(
        config_module.config, "web_ui_totp_account", "test", raising=False
    )
    monkeypatch.setattr(
        config_module.config, "web_ui_cookie_secure", "false", raising=False
    )
    monkeypatch.setattr(
        config_module.config,
        "web_ui_allowed_origins",
        ("http://testserver",),
        raising=False,
    )


@pytest.fixture
def web_password(monkeypatch: pytest.MonkeyPatch) -> str:
    _baseline_config(monkeypatch)
    return "hunter2"


@pytest.fixture
def client(web_password: str) -> TestClient:
    bus = EventBus()
    app = create_app(bus)
    return TestClient(app)


@pytest.fixture
def authed_client(client: TestClient, web_password: str) -> TestClient:
    r = client.post("/api/login", json={"password": web_password})
    assert r.status_code == 200
    return client


def test_me_unauthenticated(client: TestClient) -> None:
    r = client.get("/api/me")
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "authenticated": False,
        "enabled": True,
        "totp_required": False,
    }


def test_login_wrong_password(client: TestClient) -> None:
    r = client.post("/api/login", json={"password": "wrong"})
    assert r.status_code == 401


def test_login_sets_cookie(client: TestClient, web_password: str) -> None:
    r = client.post("/api/login", json={"password": web_password})
    assert r.status_code == 200
    assert "codexbot_session" in client.cookies
    r2 = client.get("/api/me")
    assert r2.json()["authenticated"] is True


def test_protected_endpoint_requires_auth(client: TestClient) -> None:
    r = client.get("/api/sessions")
    assert r.status_code == 401


def test_list_sessions_returns_windows(
    authed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_windows = [
        TmuxWindow(window_id="@5", window_name="codexbot", cwd="/tmp", pane_current_command="codex"),
    ]

    async def fake_list() -> list[TmuxWindow]:
        return fake_windows

    from codexbot.web import api as web_api

    monkeypatch.setattr(web_api.tmux_manager, "list_windows", fake_list)

    r = authed_client.get("/api/sessions")
    assert r.status_code == 200
    body = r.json()
    assert len(body["sessions"]) == 1
    assert body["sessions"][0]["window_id"] == "@5"
    assert body["sessions"][0]["runtime"] in {"codex", "claude"}


def test_send_text_invokes_session_manager(
    authed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str]] = []

    async def fake_send(wid: str, text: str) -> tuple[bool, str]:
        calls.append((wid, text))
        return True, "sent"

    from codexbot.web import api as web_api

    monkeypatch.setattr(web_api.session_manager, "send_to_window", fake_send)

    r = authed_client.post(
        "/api/sessions/@5/text", json={"text": "hello world", "enter": True}
    )
    assert r.status_code == 200, r.text
    assert calls == [("@5", "hello world")]


def test_send_key_rejects_unsafe_key(authed_client: TestClient) -> None:
    r = authed_client.post("/api/sessions/@5/keys", json={"key": "Hyper-DROP-TABLE"})
    assert r.status_code == 400


def test_send_key_accepts_whitelisted(
    authed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    async def fake_send(wid: str, key: str, enter: bool, literal: bool) -> bool:
        captured.update({"wid": wid, "key": key, "enter": enter, "literal": literal})
        return True

    from codexbot.web import api as web_api

    monkeypatch.setattr(web_api.tmux_manager, "send_keys", fake_send)

    r = authed_client.post("/api/sessions/@5/keys", json={"key": "Escape"})
    assert r.status_code == 200, r.text
    assert captured == {"wid": "@5", "key": "Escape", "enter": False, "literal": False}


def test_command_endpoint_prepends_slash(
    authed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []

    async def fake_send(wid: str, text: str) -> tuple[bool, str]:
        sent.append(text)
        return True, "ok"

    from codexbot.web import api as web_api

    monkeypatch.setattr(web_api.session_manager, "send_to_window", fake_send)

    r = authed_client.post("/api/sessions/@5/command", json={"command": "clear"})
    assert r.status_code == 200, r.text
    assert sent == ["/clear"]


def test_logout_clears_cookie(authed_client: TestClient) -> None:
    r = authed_client.post("/api/logout")
    assert r.status_code == 200
    me = authed_client.get("/api/me").json()
    assert me["authenticated"] is False


# ---------------------------------------------------------------------------
# Login rate-limit
# ---------------------------------------------------------------------------


def test_login_rate_limit_after_repeated_failures(client: TestClient) -> None:
    # Configured limit is 5 failures per 5 minutes. The 6th attempt must be
    # rejected with 429 even if the password were correct.
    for _ in range(5):
        r = client.post("/api/login", json={"password": "wrong"})
        assert r.status_code == 401
    r = client.post("/api/login", json={"password": "hunter2"})
    assert r.status_code == 429


def test_login_success_clears_rate_limit(client: TestClient) -> None:
    for _ in range(3):
        client.post("/api/login", json={"password": "wrong"})
    # A successful login resets the counter so the next session can retry.
    r = client.post("/api/login", json={"password": "hunter2"})
    assert r.status_code == 200
    # Drop the cookie and exhaust 5 fresh failures — they should be accepted
    # as attempts rather than blocked from the prior 3.
    client.cookies.clear()
    for _ in range(5):
        assert client.post("/api/login", json={"password": "wrong"}).status_code == 401


# ---------------------------------------------------------------------------
# TOTP
# ---------------------------------------------------------------------------


@pytest.fixture
def totp_secret(monkeypatch: pytest.MonkeyPatch) -> str:
    secret = pyotp.random_base32()
    monkeypatch.setattr(
        config_module.config, "web_ui_totp_required", True, raising=False
    )
    monkeypatch.setattr(
        config_module.config, "web_ui_totp_secret", secret, raising=False
    )
    return secret


@pytest.fixture
def client_with_totp(web_password: str, totp_secret: str) -> TestClient:
    bus = EventBus()
    app = create_app(bus)
    return TestClient(app)


def test_me_reports_totp_required(client_with_totp: TestClient) -> None:
    body = client_with_totp.get("/api/me").json()
    assert body["totp_required"] is True


def test_login_requires_totp_when_enabled(
    client_with_totp: TestClient, web_password: str
) -> None:
    r = client_with_totp.post("/api/login", json={"password": web_password})
    assert r.status_code == 401
    assert "2fa" in r.json()["detail"].lower()


def test_login_with_valid_totp(
    client_with_totp: TestClient, web_password: str, totp_secret: str
) -> None:
    code = pyotp.TOTP(totp_secret).now()
    r = client_with_totp.post(
        "/api/login", json={"password": web_password, "totp_code": code}
    )
    assert r.status_code == 200, r.text


def test_login_with_wrong_totp_rejected(
    client_with_totp: TestClient, web_password: str
) -> None:
    r = client_with_totp.post(
        "/api/login", json={"password": web_password, "totp_code": "000000"}
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------


def test_security_headers_applied(client: TestClient) -> None:
    r = client.get("/api/me")
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "content-security-policy" in r.headers


# ---------------------------------------------------------------------------
# Cookie Secure flag
# ---------------------------------------------------------------------------


def test_cookie_secure_forced_on(
    monkeypatch: pytest.MonkeyPatch, web_password: str
) -> None:
    monkeypatch.setattr(
        config_module.config, "web_ui_cookie_secure", "true", raising=False
    )
    bus = EventBus()
    client = TestClient(create_app(bus))
    r = client.post("/api/login", json={"password": web_password})
    assert r.status_code == 200
    set_cookie = r.headers.get("set-cookie", "")
    assert "Secure" in set_cookie


# ---------------------------------------------------------------------------
# WebSocket Origin allowlist
# ---------------------------------------------------------------------------


def test_ws_rejects_foreign_origin(authed_client: TestClient) -> None:
    with pytest.raises(Exception):
        with authed_client.websocket_connect(
            "/api/ws", headers={"origin": "https://evil.com"}
        ):
            pass


def test_ws_accepts_allowed_origin(authed_client: TestClient) -> None:
    with authed_client.websocket_connect(
        "/api/ws", headers={"origin": "http://testserver"}
    ) as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"


# Re-export `json` so the import is not flagged as unused (it documents intent).
_ = json
