"""Smoke tests for the FastAPI web transport.

These exercise the HTTP plumbing — auth gates, request bodies, response
shapes — by stubbing the global `tmux_manager` and `session_manager`
singletons. The backend dependencies (libtmux, transcript files) are
not exercised here.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pyotp
import pytest
from fastapi.testclient import TestClient

from codexbot import config as config_module
from codexbot.session import CodexSession, HistorySnapshot, WindowState
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
    monkeypatch.setattr(config_module.config, "web_ui_totp_secret", "", raising=False)
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
        TmuxWindow(
            window_id="@5",
            window_name="codexbot",
            cwd="/tmp",
            pane_current_command="codex",
        ),
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
    assert "sort_order" in body["sessions"][0]


def test_list_sessions_uses_manual_order_and_keeps_pinned_first(
    authed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_windows = [
        TmuxWindow(
            window_id="@1",
            window_name="one",
            cwd="/tmp/one",
            pane_current_command="codex",
        ),
        TmuxWindow(
            window_id="@2",
            window_name="two",
            cwd="/tmp/two",
            pane_current_command="codex",
        ),
        TmuxWindow(
            window_id="@3",
            window_name="three",
            cwd="/tmp/three",
            pane_current_command="codex",
        ),
    ]

    async def fake_list() -> list[TmuxWindow]:
        return fake_windows

    from codexbot.web import api as web_api

    monkeypatch.setattr(web_api.tmux_manager, "list_windows", fake_list)
    monkeypatch.setattr(
        web_api.session_manager,
        "_refresh_sessions_index",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        web_api.session_manager,
        "window_states",
        {
            "@1": WindowState(session_id="s1", sort_order=2),
            "@2": WindowState(session_id="s2", sort_order=0),
            "@3": WindowState(session_id="s3", pinned=True, sort_order=99),
        },
    )
    monkeypatch.setattr(
        web_api.session_manager,
        "_session_mtime_index",
        {"s1": 100.0, "s2": 200.0, "s3": 50.0},
    )

    r = authed_client.get("/api/sessions")

    assert r.status_code == 200, r.text
    assert [s["window_id"] for s in r.json()["sessions"]] == ["@3", "@2", "@1"]


def test_reorder_sessions_persists_sort_order(
    authed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_windows = [
        TmuxWindow(
            window_id="@1",
            window_name="one",
            cwd="/tmp/one",
            pane_current_command="codex",
        ),
        TmuxWindow(
            window_id="@2",
            window_name="two",
            cwd="/tmp/two",
            pane_current_command="codex",
        ),
        TmuxWindow(
            window_id="@3",
            window_name="three",
            cwd="/tmp/three",
            pane_current_command="codex",
        ),
    ]
    saved: list[bool] = []

    async def fake_list() -> list[TmuxWindow]:
        return fake_windows

    from codexbot.web import api as web_api

    monkeypatch.setattr(web_api.tmux_manager, "list_windows", fake_list)
    monkeypatch.setattr(
        web_api.session_manager,
        "window_states",
        {"@1": WindowState(), "@2": WindowState(), "@3": WindowState()},
    )
    monkeypatch.setattr(
        web_api.session_manager,
        "_save_state",
        lambda: saved.append(True),
    )

    r = authed_client.patch(
        "/api/sessions/order",
        json={"window_ids": ["@3", "@1", "@2"]},
    )

    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}
    assert web_api.session_manager.window_states["@3"].sort_order == 0
    assert web_api.session_manager.window_states["@1"].sort_order == 1
    assert web_api.session_manager.window_states["@2"].sort_order == 2
    assert saved == [True]


def test_reorder_sessions_rejects_duplicate_window_ids(
    authed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_list() -> list[TmuxWindow]:
        return [
            TmuxWindow(
                window_id="@1",
                window_name="one",
                cwd="/tmp/one",
                pane_current_command="codex",
            )
        ]

    from codexbot.web import api as web_api

    monkeypatch.setattr(web_api.tmux_manager, "list_windows", fake_list)

    r = authed_client.patch("/api/sessions/order", json={"window_ids": ["@1", "@1"]})

    assert r.status_code == 400


def test_get_messages_returns_history_metadata(
    authed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codexbot.web import api as web_api

    snapshot = HistorySnapshot(
        messages=[
            {
                "role": "assistant",
                "text": "hello",
                "content_type": "tool_use",
                "timestamp": "2026-05-19T10:00:00Z",
                "tool_name": "request_user_input",
                "tool_input": {
                    "questions": [
                        {
                            "question": "Choose rollout mode",
                            "options": [{"label": "Canary"}],
                        }
                    ]
                },
                "tool_use_id": "prompt-1",
            }
        ],
        total_count=1,
        oldest_timestamp="2026-05-19T10:00:00Z",
        newest_timestamp="2026-05-19T10:00:00Z",
        history_version="123:456:1",
    )
    session = CodexSession("session-1", "hello", 1, "/tmp/session.jsonl")
    monkeypatch.setattr(
        web_api.session_manager,
        "get_history_snapshot",
        AsyncMock(return_value=snapshot),
    )
    monkeypatch.setattr(
        web_api.session_manager,
        "resolve_session_for_window",
        AsyncMock(return_value=session),
    )

    r = authed_client.get("/api/sessions/@5/messages")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {
        "messages": snapshot.messages,
        "session_id": "session-1",
        "has_more": False,
        "oldest_timestamp": "2026-05-19T10:00:00Z",
        "newest_timestamp": "2026-05-19T10:00:00Z",
        "history_version": "123:456:1",
    }


def test_get_messages_filters_by_transcript_order(
    authed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codexbot.web import api as web_api

    messages = [
        {
            "role": "assistant",
            "text": "first",
            "content_type": "text",
            "timestamp": "2026-05-19T10:00:00Z",
            "transcript_offset": 10,
            "transcript_index": 0,
        },
        {
            "role": "assistant",
            "text": "second",
            "content_type": "tool_use",
            "timestamp": "2026-05-19T10:00:00Z",
            "transcript_offset": 10,
            "transcript_index": 1,
        },
        {
            "role": "assistant",
            "text": "third",
            "content_type": "text",
            "timestamp": "2026-05-19T10:00:00Z",
            "transcript_offset": 20,
            "transcript_index": 0,
        },
    ]
    snapshot = HistorySnapshot(
        messages=messages,
        total_count=len(messages),
        oldest_timestamp="2026-05-19T10:00:00Z",
        newest_timestamp="2026-05-19T10:00:00Z",
        history_version="20:456:3",
    )
    session = CodexSession("session-1", "hello", len(messages), "/tmp/session.jsonl")
    monkeypatch.setattr(
        web_api.session_manager,
        "get_history_snapshot",
        AsyncMock(return_value=snapshot),
    )
    monkeypatch.setattr(
        web_api.session_manager,
        "resolve_session_for_window",
        AsyncMock(return_value=session),
    )

    r = authed_client.get("/api/sessions/@5/messages?after_offset=10&after_index=0")

    assert r.status_code == 200, r.text
    assert [m["text"] for m in r.json()["messages"]] == ["second", "third"]


def test_get_slash_commands_returns_registry_response(
    authed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codexbot.web import api as web_api

    calls: list[dict[str, str | None]] = []

    class CommandSet:
        def to_response(self) -> dict[str, Any]:
            return {
                "runtime": "claude",
                "window_id": "@5",
                "session_id": "session-1",
                "commands": [
                    {"command": "/clear", "description": "Clear conversation"}
                ],
                "source": "discovered",
                "updated_at": 1.0,
            }

    def fake_get_commands(
        runtime: str | None,
        *,
        window_id: str | None = None,
        session_id: str | None = None,
    ) -> CommandSet:
        calls.append(
            {"runtime": runtime, "window_id": window_id, "session_id": session_id}
        )
        return CommandSet()

    state = web_api.session_manager.get_window_state("@5")
    state.runtime = "claude"
    state.session_id = "session-1"
    monkeypatch.setattr(
        web_api.slash_command_registry,
        "get_commands",
        fake_get_commands,
    )

    r = authed_client.get("/api/slash-commands?window_id=@5")
    assert r.status_code == 200, r.text
    assert r.json()["commands"] == [
        {"command": "/clear", "description": "Clear conversation"}
    ]
    assert calls == [
        {"runtime": "claude", "window_id": "@5", "session_id": "session-1"}
    ]


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


def test_persistent_shell_session_name_is_stable_and_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codexbot.web import api as web_api

    monkeypatch.setattr(web_api.tmux_manager, "session_name", "codex:bot")

    assert web_api._persistent_shell_session_name("@34") == "codex_bot-shell-34"


@pytest.mark.asyncio
async def test_ensure_persistent_shell_reuses_existing_session(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codexbot.web import api as web_api

    monkeypatch.setattr(web_api.tmux_manager, "session_name", "codexbot")
    run_calls: list[list[str]] = []

    async def fake_exists(session_name: str) -> bool:
        assert session_name == "codexbot-shell-5"
        return True

    async def fake_run(args: list[str], *, timeout: float = 3.0) -> int:
        run_calls.append(args)
        return 0

    monkeypatch.setattr(web_api, "_tmux_session_exists", fake_exists)
    monkeypatch.setattr(web_api, "_run_tmux_command", fake_run)

    session_name = await web_api._ensure_persistent_shell_session("@5", str(tmp_path))

    assert session_name == "codexbot-shell-5"
    assert run_calls == []


@pytest.mark.asyncio
async def test_ensure_persistent_shell_creates_session_in_window_cwd(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codexbot.web import api as web_api

    monkeypatch.setattr(web_api.tmux_manager, "session_name", "codexbot")
    run_calls: list[tuple[list[str], float]] = []

    async def fake_exists(_session_name: str) -> bool:
        return False

    async def fake_run(args: list[str], *, timeout: float = 3.0) -> int:
        run_calls.append((args, timeout))
        return 0

    monkeypatch.setattr(web_api, "_tmux_session_exists", fake_exists)
    monkeypatch.setattr(web_api, "_run_tmux_command", fake_run)

    session_name = await web_api._ensure_persistent_shell_session("@5", str(tmp_path))

    assert session_name == "codexbot-shell-5"
    assert run_calls == [
        (
            [
                "new-session",
                "-d",
                "-s",
                "codexbot-shell-5",
                "-c",
                str(tmp_path),
            ],
            5.0,
        )
    ]


def test_kill_session_cleans_persistent_shell(
    authed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codexbot.web import api as web_api

    async def fake_kill_window(window_id: str) -> bool:
        assert window_id == "@5"
        return True

    shell_kill = AsyncMock(return_value=True)
    monkeypatch.setattr(web_api.tmux_manager, "kill_window", fake_kill_window)
    monkeypatch.setattr(web_api, "_kill_persistent_shell_session", shell_kill)

    r = authed_client.delete("/api/sessions/@5")

    assert r.status_code == 200, r.text
    shell_kill.assert_awaited_once_with("@5")


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
