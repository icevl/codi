"""FastAPI app exposing CodexBot operations over HTTP + WebSocket.

Endpoints (all under `/api` unless stated):

  POST /api/login                    {password} → 200 + set-cookie
  POST /api/logout                   clears cookie
  GET  /api/me                       current auth state
  GET  /api/sessions                 list windows + bound runtime/session
  POST /api/sessions                 create new window (cwd, runtime[, resume])
  DELETE /api/sessions/{wid}         kill window
  PATCH  /api/sessions/{wid}         rename window
  GET  /api/sessions/{wid}/messages?limit=N&before=ISO&after=ISO → paginated history
  GET  /api/sessions/{wid}/git       {is_repo, branch} for the pane's cwd
  GET  /api/sessions/{wid}/branches  {is_repo, current, branches[]} — local heads
  POST /api/sessions/{wid}/switch-branch {branch} — runs `git switch`
  GET  /api/sessions/{wid}/diff       uncommitted diff vs HEAD + untracked list
  POST /api/sessions/{wid}/text      {text, enter?, armed_skill?}
  POST /api/sessions/{wid}/keys      {key} — Escape, Up, Down, Enter, C-c, …
  POST /api/sessions/{wid}/command   {command} — forwards "/clear", "/new" etc.
  GET  /api/sessions/{wid}/screenshot.png
  GET  /api/directories?path=…       directory browser
  GET  /api/resume-sessions?cwd=…    list resumable runtime sessions
  GET  /api/runtimes                 list available runtimes
  GET  /api/skills?runtime=…         skill names for runtime
  WS   /api/ws                       server→client event stream

Static assets (the built React app) are served from `web-ui/dist/` if present.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..config import config
from ..runtimes import all_runtimes, get_runtime
from ..session import session_manager
from ..skills import discover_skills
from ..tmux_manager import tmux_manager
from ..utils import codexbot_dir
from .auth import (
    COOKIE_NAME,
    Authenticator,
    AuthConfig,
    resolve_cookie_secure,
    set_session_cookie,
)
from .events import EventBus
from .screenshot_helper import capture_screenshot

if TYPE_CHECKING:
    from telegram import Bot

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic request bodies
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    password: str
    totp_code: str | None = None


class CreateSessionRequest(BaseModel):
    cwd: str
    runtime: str = "codex"
    resume_session_id: str | None = None
    name: str | None = None


class PatchSessionRequest(BaseModel):
    """PATCH /api/sessions/{wid} body. At least one field must be supplied."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    pinned: bool | None = None


class SendTextRequest(BaseModel):
    text: str
    enter: bool = True


class SendKeyRequest(BaseModel):
    key: str


class SendCommandRequest(BaseModel):
    command: str


class SwitchBranchRequest(BaseModel):
    branch: str = Field(min_length=1, max_length=255)


# Cap for the image-upload endpoint. Telegram bot accepts up to 20 MB photos,
# matching that here keeps the two transports consistent.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

# (magic-bytes prefix, file extension) — used to sniff the format when the
# browser doesn't supply a usable filename. Order matters: longer prefixes
# first so PNG isn't mis-detected.
_IMAGE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"RIFF", ".webp"),  # WEBP starts with "RIFF....WEBP"; "RIFF" is enough to gate
)

_ALLOWED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def _sniff_image_ext(body: bytes) -> str | None:
    for prefix, ext in _IMAGE_MAGIC:
        if body.startswith(prefix):
            if ext == ".webp" and b"WEBP" not in body[:16]:
                continue
            return ext
    return None


def _safe_image_ext(filename: str) -> str | None:
    ext = Path(filename).suffix.lower()
    if ext in _ALLOWED_IMAGE_EXTS:
        return ".jpg" if ext == ".jpeg" else ext
    return None


# Special key names whitelisted for /keys. The tmux send-keys protocol accepts
# many tokens, but we restrict to a curated, safe set.
ALLOWED_KEYS: set[str] = {
    "Enter",
    "Escape",
    "Tab",
    "BTab",
    "Space",
    "Up",
    "Down",
    "Left",
    "Right",
    "PageUp",
    "PageDown",
    "Home",
    "End",
    "BSpace",
    "C-c",
    "C-d",
    "C-z",
    "C-l",
    "C-a",
    "C-e",
    "C-u",
    "C-r",
}


def _resolve_mirror_target() -> tuple[int, int] | None:
    """Pick `(user_id, chat_id)` for Telegram-topic mirroring, if available.

    Mirroring needs both an allowed user and a group chat the bot has seen
    that user post in (otherwise we don't know which forum to create the
    topic in). We pick the first allowed user that has a stored group chat
    id — for single-user setups this is unambiguous.
    """
    if not config.allowed_users:
        return None
    # group_chat_ids is keyed `f"{user_id}:{thread_id}"` and gets populated
    # whenever the bot sees a message in a forum group. Any entry for an
    # allowed user gives us the chat id we need — resolve_chat_id requires
    # a known thread_id, which we don't have yet at session-creation time.
    for user_id in config.allowed_users:
        prefix = f"{user_id}:"
        for key, chat_id in session_manager.group_chat_ids.items():
            if key.startswith(prefix) and chat_id and chat_id != user_id:
                return user_id, chat_id
    return None


async def _create_telegram_topic(
    bot: "Bot | None", window_id: str, window_name: str
) -> tuple[int | None, str | None]:
    """Create a forum topic mirroring this window. Returns (thread_id, err)."""
    if bot is None:
        return None, None
    target = _resolve_mirror_target()
    if target is None:
        return None, "no telegram group on file — post in the group once first"
    user_id, chat_id = target
    try:
        topic = await bot.create_forum_topic(chat_id=chat_id, name=window_name)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "create_forum_topic failed chat=%s window=%s: %s",
            chat_id,
            window_id,
            exc,
        )
        return None, str(exc)
    thread_id = topic.message_thread_id
    session_manager.set_group_chat_id(user_id, thread_id, chat_id)
    session_manager.bind_thread(user_id, thread_id, window_id, window_name=window_name)
    logger.info(
        "Telegram topic mirrored: window=%s thread=%s name=%r",
        window_id,
        thread_id,
        window_name,
    )
    return thread_id, None


async def _rename_telegram_topic(
    bot: "Bot | None", window_id: str, new_name: str
) -> None:
    if bot is None:
        return
    for user_id, thread_id, wid in session_manager.iter_thread_bindings():
        if wid != window_id:
            continue
        chat_id = session_manager.resolve_chat_id(user_id, thread_id)
        try:
            await bot.edit_forum_topic(
                chat_id=chat_id, message_thread_id=thread_id, name=new_name
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "edit_forum_topic failed window=%s thread=%s: %s",
                window_id,
                thread_id,
                exc,
            )
        return


async def _delete_telegram_topic(bot: "Bot | None", window_id: str) -> None:
    if bot is None:
        return
    # Snapshot bindings because deletion mutates the underlying dict.
    bindings = [
        (uid, tid)
        for uid, tid, wid in session_manager.iter_thread_bindings()
        if wid == window_id
    ]
    for user_id, thread_id in bindings:
        chat_id = session_manager.resolve_chat_id(user_id, thread_id)
        try:
            await bot.delete_forum_topic(chat_id=chat_id, message_thread_id=thread_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "delete_forum_topic failed window=%s thread=%s: %s",
                window_id,
                thread_id,
                exc,
            )
        session_manager.unbind_thread(user_id, thread_id)


def create_app(
    bus: EventBus, *, dev_mode: bool = False, bot: "Bot | None" = None
) -> FastAPI:
    """Build the FastAPI app with the shared event bus.

    When `bot` is provided, /api/sessions create/rename/delete operations
    mirror to a Telegram forum topic so the two transports stay in sync.
    """
    # Origin allowlist also includes Vite dev origins when running locally.
    allowed_origins = tuple(config.web_ui_allowed_origins)
    if dev_mode:
        dev_origins = ("http://127.0.0.1:5173", "http://localhost:5173")
        allowed_origins = tuple({*allowed_origins, *dev_origins})
    auth = Authenticator(
        AuthConfig(
            password=config.web_ui_password,
            secret=config.web_ui_secret,
            totp_secret=config.web_ui_totp_secret
            if config.web_ui_totp_required
            else "",
            totp_issuer=config.web_ui_totp_issuer,
            totp_account=config.web_ui_totp_account,
            cookie_secure_mode=config.web_ui_cookie_secure,
            allowed_origins=allowed_origins,
        )
    )
    app = FastAPI(title="CodexBot Web UI", version="0.1.0")
    # Stash on the app so tests / server bootstrap can introspect.
    app.state.authenticator = auth

    if dev_mode:
        # Vite dev server runs on a different port; allow it during development.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # -----------------------------------------------------------------------
    # Security headers
    # -----------------------------------------------------------------------

    @app.middleware("http")
    async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        # Defense-in-depth: prevent framing/clickjacking, sniffing, referrer
        # leaks. CSP intentionally permissive so the SPA bundle still loads;
        # tighten when adding external CDNs.
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob:; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "script-src 'self'; "
            "connect-src 'self' ws: wss:; frame-ancestors 'none'",
        )
        return response

    # -----------------------------------------------------------------------
    # Auth helpers
    # -----------------------------------------------------------------------

    async def require_auth(request: Request) -> str:
        cookie = request.cookies.get(COOKIE_NAME)
        subject = auth.verify_cookie(cookie)
        if not subject:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized"
            )
        return subject

    # -----------------------------------------------------------------------
    # Login rate-limit — sliding window per client IP. State is in-memory;
    # this is a self-hosted single-process service, so we don't need to
    # share across instances.
    # -----------------------------------------------------------------------

    login_failures: dict[str, list[float]] = {}
    LOGIN_WINDOW_SECONDS = 300.0  # 5 minutes
    LOGIN_MAX_FAILURES = 5
    LOGIN_FAILURE_DELAY = 0.5  # always sleep this long on failure

    def _client_ip(request: Request) -> str:
        # `request.client.host` is the immediate peer; deployments behind a
        # trusted reverse proxy can carry the real IP in X-Forwarded-For.
        # Only the first hop is honored to avoid spoofing.
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",", 1)[0].strip() or "unknown"
        return request.client.host if request.client else "unknown"

    def _login_locked(ip: str) -> bool:
        now = time.monotonic()
        bucket = login_failures.get(ip)
        if not bucket:
            return False
        cutoff = now - LOGIN_WINDOW_SECONDS
        fresh = [t for t in bucket if t >= cutoff]
        if fresh:
            login_failures[ip] = fresh
        else:
            login_failures.pop(ip, None)
        return len(fresh) >= LOGIN_MAX_FAILURES

    def _login_record_failure(ip: str) -> None:
        login_failures.setdefault(ip, []).append(time.monotonic())

    def _login_clear(ip: str) -> None:
        login_failures.pop(ip, None)

    # -----------------------------------------------------------------------
    # Auth endpoints
    # -----------------------------------------------------------------------

    @app.post("/api/login")
    async def login(
        req: LoginRequest, request: Request, response: Response
    ) -> dict[str, Any]:
        if not auth.enabled:
            raise HTTPException(
                status_code=503, detail="web UI disabled (no WEB_UI_PASSWORD set)"
            )

        ip = _client_ip(request)
        if _login_locked(ip):
            # Don't reveal whether the credentials would have been valid.
            await asyncio.sleep(LOGIN_FAILURE_DELAY)
            raise HTTPException(
                status_code=429, detail="too many login attempts, try again later"
            )

        password_ok = auth.check_password(req.password)
        totp_ok = auth.check_totp(req.totp_code or "") if auth.totp_enabled else True

        if not (password_ok and totp_ok):
            _login_record_failure(ip)
            # Constant-ish delay so attackers can't distinguish "wrong
            # password" from "wrong code" by timing.
            await asyncio.sleep(LOGIN_FAILURE_DELAY)
            if auth.totp_enabled and password_ok and not totp_ok:
                raise HTTPException(status_code=401, detail="invalid 2FA code")
            raise HTTPException(status_code=401, detail="invalid password")

        _login_clear(ip)
        cookie_value = auth.mint_cookie("user")
        secure = resolve_cookie_secure(
            auth.cookie_secure_mode, request.url.scheme == "https"
        )
        set_session_cookie(response, cookie_value, secure=secure)
        return {"ok": True}

    @app.post("/api/logout")
    async def logout(response: Response) -> dict[str, Any]:
        response.delete_cookie(COOKIE_NAME, path="/")
        return {"ok": True}

    @app.get("/api/me")
    async def me(request: Request) -> dict[str, Any]:
        cookie = request.cookies.get(COOKIE_NAME)
        subject = auth.verify_cookie(cookie)
        return {
            "authenticated": bool(subject),
            "enabled": auth.enabled,
            "totp_required": auth.totp_enabled,
        }

    # -----------------------------------------------------------------------
    # Sessions
    # -----------------------------------------------------------------------

    @app.get("/api/sessions")
    async def list_sessions(_user: str = Depends(require_auth)) -> dict[str, Any]:
        windows = await tmux_manager.list_windows()
        # Make sure the transcript mtime index is current so the sort by
        # activity reflects reality.
        await session_manager._refresh_sessions_index(force=True)
        result: list[dict[str, Any]] = []
        for w in windows:
            ws = session_manager.get_window_state(w.window_id)
            runtime_name = ws.runtime or "codex"
            display_name = (
                session_manager.get_display_name(w.window_id) or w.window_name
            )
            last_activity: float | None = None
            if ws.session_id:
                mtime = session_manager._session_mtime_index.get(ws.session_id)
                if mtime:
                    last_activity = mtime
            result.append(
                {
                    "window_id": w.window_id,
                    "name": display_name,
                    "tmux_name": w.window_name,
                    "cwd": ws.cwd or w.cwd,
                    "runtime": runtime_name,
                    "session_id": ws.session_id or None,
                    "pane_command": w.pane_current_command,
                    "last_activity": last_activity,
                    "pinned": bool(ws.pinned),
                }
            )
        # Pinned sessions float to the top; within each group, hot sessions
        # first, then name for a stable tie-breaker.
        result.sort(
            key=lambda s: (
                0 if s["pinned"] else 1,
                -(s["last_activity"] or 0.0),
                s["name"].lower(),
            )
        )
        return {"sessions": result}

    @app.post("/api/sessions")
    async def create_session(
        req: CreateSessionRequest,
        _user: str = Depends(require_auth),
    ) -> dict[str, Any]:
        runtime = get_runtime(req.runtime)
        path = Path(req.cwd).expanduser()
        if not path.is_absolute():
            path = path.resolve()
        if not path.exists() or not path.is_dir():
            raise HTTPException(400, detail=f"directory not found: {path}")

        success, message, wname, wid = await tmux_manager.create_window(
            str(path),
            window_name=req.name,
            resume_session_id=req.resume_session_id,
            runtime=runtime,
        )
        if not success:
            raise HTTPException(400, detail=message)

        ws = session_manager.get_window_state(wid)
        ws.runtime = runtime.name
        ws.cwd = str(path)
        ws.window_name = wname
        session_manager._save_state()

        if runtime.name == "claude":
            pane_pid = await tmux_manager.get_pane_pid(wid)
            sid = await runtime.discover_session_id(
                window_id=wid,
                pane_pid=pane_pid,
                cwd=str(path),
                allow_cwd_fallback=True,
            )
            if sid:
                ws.session_id = sid
                session_manager._save_state()
        else:
            if not req.resume_session_id:
                session_manager.mark_window_for_new_session(wid, clear_existing=False)
            detect_timeout = 15.0 if req.resume_session_id else 5.0
            try:
                await session_manager.wait_for_session_map_entry(
                    wid, timeout=detect_timeout
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("session detect raised %s", exc)
            if req.resume_session_id:
                ws = session_manager.get_window_state(wid)
                ws.session_id = req.resume_session_id
                session_manager._save_state()

        # Mirror to Telegram (best-effort — tmux session is fine even if
        # the forum topic can't be created).
        thread_id, mirror_err = await _create_telegram_topic(bot, wid, wname)

        await bus.publish_sessions_changed()
        return {
            "window_id": wid,
            "name": wname,
            "cwd": str(path),
            "runtime": runtime.name,
            "session_id": ws.session_id or None,
            "telegram_thread_id": thread_id,
            "telegram_mirror_error": mirror_err,
        }

    @app.delete("/api/sessions/{window_id}")
    async def kill_session(
        window_id: str, _user: str = Depends(require_auth)
    ) -> dict[str, Any]:
        ok = await tmux_manager.kill_window(window_id)
        if not ok:
            raise HTTPException(404, detail="window not found")
        # Delete the matching Telegram topic before dropping local state,
        # otherwise iter_thread_bindings has already lost the mapping.
        await _delete_telegram_topic(bot, window_id)
        session_manager.window_states.pop(window_id, None)
        session_manager.window_display_names.pop(window_id, None)
        session_manager._save_state()
        await bus.publish_sessions_changed()
        return {"ok": True}

    @app.patch("/api/sessions/{window_id}")
    async def patch_session(
        window_id: str,
        req: PatchSessionRequest,
        _user: str = Depends(require_auth),
    ) -> dict[str, Any]:
        if req.name is None and req.pinned is None:
            raise HTTPException(400, detail="no fields to update")

        ws = session_manager.window_states.get(window_id)
        w = await tmux_manager.find_window_by_id(window_id)
        if not w and ws is None:
            raise HTTPException(404, detail="window not found")

        response: dict[str, Any] = {"ok": True}

        if req.name is not None:
            new_name = req.name.strip()
            if not new_name:
                raise HTTPException(400, detail="empty name")
            ok = await tmux_manager.rename_window(window_id, new_name)
            if not ok:
                raise HTTPException(404, detail="window not found")
            session_manager.update_display_name(window_id, new_name)
            await _rename_telegram_topic(bot, window_id, new_name)
            response["name"] = new_name

        if req.pinned is not None:
            state = session_manager.get_window_state(window_id)
            state.pinned = bool(req.pinned)
            session_manager._save_state()
            response["pinned"] = state.pinned

        await bus.publish_sessions_changed()
        return response

    @app.get("/api/sessions/{window_id}/messages")
    async def get_messages(
        window_id: str,
        limit: int = Query(500, ge=1, le=2000),
        before: str | None = Query(None),
        after: str | None = Query(None),
        _user: str = Depends(require_auth),
    ) -> dict[str, Any]:
        history = await session_manager.get_history_snapshot(window_id)
        all_messages = history.messages
        session = await session_manager.resolve_session_for_window(window_id)

        # ISO timestamps compare lexicographically.
        if before is not None:
            all_messages = [
                m for m in all_messages if (m.get("timestamp") or "") < before
            ]
        if after is not None:
            all_messages = [
                m for m in all_messages if (m.get("timestamp") or "") > after
            ]

        has_more = len(all_messages) > limit
        messages = all_messages[-limit:]

        return {
            "messages": messages,
            "session_id": session.session_id if session else None,
            "has_more": has_more,
            "oldest_timestamp": history.oldest_timestamp,
            "newest_timestamp": history.newest_timestamp,
            "history_version": history.history_version,
        }

    async def _resolve_window_cwd(window_id: str) -> str:
        # Prefer the live pane cwd — if the user `cd`d inside the shell,
        # that's the repo we want to reflect. Fall back to the recorded
        # session cwd when the window has gone away (race with kill).
        w = await tmux_manager.find_window_by_id(window_id)
        cwd = (w.cwd if w else "") or ""
        if not cwd:
            ws = session_manager.window_states.get(window_id)
            cwd = (ws.cwd if ws else "") or ""
        return cwd

    @app.get("/api/sessions/{window_id}/git")
    async def get_git_info(
        window_id: str, _user: str = Depends(require_auth)
    ) -> dict[str, Any]:
        cwd = await _resolve_window_cwd(window_id)
        if not cwd:
            return {"is_repo": False, "branch": None}
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                cwd,
                "rev-parse",
                "--abbrev-ref",
                "HEAD",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=1.5)
        except (FileNotFoundError, asyncio.TimeoutError):
            return {"is_repo": False, "branch": None}
        if proc.returncode != 0:
            return {"is_repo": False, "branch": None}
        branch = stdout.decode("utf-8", errors="replace").strip() or None
        return {"is_repo": branch is not None, "branch": branch}

    @app.get("/api/sessions/{window_id}/branches")
    async def list_branches(
        window_id: str, _user: str = Depends(require_auth)
    ) -> dict[str, Any]:
        cwd = await _resolve_window_cwd(window_id)
        if not cwd:
            return {"is_repo": False, "current": None, "branches": []}
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                cwd,
                "for-each-ref",
                "--sort=-committerdate",
                "--format=%(refname:short)",
                "refs/heads/",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
        except (FileNotFoundError, asyncio.TimeoutError):
            return {"is_repo": False, "current": None, "branches": []}
        if proc.returncode != 0:
            return {"is_repo": False, "current": None, "branches": []}
        branches = [
            line
            for line in stdout.decode("utf-8", errors="replace").splitlines()
            if line
        ]
        # Reuse the same single-shot command as /git so the "current" marker
        # in the dropdown stays consistent.
        head_proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            cwd,
            "rev-parse",
            "--abbrev-ref",
            "HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        head_out, _ = await head_proc.communicate()
        current = head_out.decode("utf-8", errors="replace").strip() or None
        return {"is_repo": True, "current": current, "branches": branches}

    @app.post("/api/sessions/{window_id}/switch-branch")
    async def switch_branch(
        window_id: str,
        req: SwitchBranchRequest,
        _user: str = Depends(require_auth),
    ) -> dict[str, Any]:
        cwd = await _resolve_window_cwd(window_id)
        if not cwd:
            raise HTTPException(404, detail="window has no cwd")
        # `git switch` validates the branch name itself and refuses anything
        # weird (spaces, leading dashes, etc.), so we just pass the value
        # straight through after stripping whitespace.
        branch = req.branch.strip()
        if not branch:
            raise HTTPException(400, detail="empty branch name")
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                cwd,
                "switch",
                "--",
                branch,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        except FileNotFoundError:
            raise HTTPException(500, detail="git not found")
        except asyncio.TimeoutError:
            raise HTTPException(504, detail="git switch timed out")
        if proc.returncode != 0:
            # Surface stderr so the UI can show "your local changes would be
            # overwritten…" or similar verbatim — that's the actionable bit.
            msg = (
                stderr.decode("utf-8", errors="replace").strip() or "git switch failed"
            )
            raise HTTPException(409, detail=msg)
        return {
            "ok": True,
            "branch": branch,
            "stdout": stdout.decode("utf-8", errors="replace").strip(),
        }

    @app.get("/api/sessions/{window_id}/diff")
    async def get_diff(
        window_id: str,
        request: Request,
        response: Response,
        _user: str = Depends(require_auth),
    ) -> Any:
        cwd = await _resolve_window_cwd(window_id)
        empty = {
            "is_repo": False,
            "diff": "",
            "additions": 0,
            "deletions": 0,
            "file_count": 0,
            "untracked": [],
        }
        if not cwd:
            return empty
        try:
            # `git diff HEAD` captures staged + unstaged changes. We also
            # collect untracked names separately so they show up in the panel
            # even though they have no diff.
            diff_proc = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                cwd,
                "diff",
                "HEAD",
                "--no-color",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            untracked_proc = await asyncio.create_subprocess_exec(
                "git",
                "-C",
                cwd,
                "ls-files",
                "--others",
                "--exclude-standard",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            diff_bytes, _ = await asyncio.wait_for(diff_proc.communicate(), timeout=5.0)
            untracked_bytes, _ = await asyncio.wait_for(
                untracked_proc.communicate(), timeout=2.0
            )
        except (FileNotFoundError, asyncio.TimeoutError):
            return empty
        if diff_proc.returncode != 0:
            # 128 = not a repo / no HEAD. Treat as empty rather than erroring.
            return empty

        diff_text = diff_bytes.decode("utf-8", errors="replace")
        untracked = [
            line
            for line in untracked_bytes.decode("utf-8", errors="replace").splitlines()
            if line
        ]
        # Weak ETag over diff text + untracked list. Weak (`W/`) because we
        # don't promise byte-for-byte representation equality — the JSON
        # envelope keys could be reordered by serializer changes, but the
        # *content* is stable, which is all the client cares about.
        digest = hashlib.sha1(
            (diff_text + "\n\x00\n" + "\n".join(untracked)).encode("utf-8"),
            usedforsecurity=False,
        ).hexdigest()
        etag = f'W/"{digest}"'

        if request.headers.get("if-none-match") == etag:
            # No body on 304 — saves the diff payload (can be MB on big
            # WIPs) when the client already has the latest snapshot.
            return Response(status_code=304, headers={"ETag": etag})

        # Cheap stats by scanning the diff lines. Skips diff/index/+++/--- headers.
        additions = 0
        deletions = 0
        file_count = 0
        for line in diff_text.splitlines():
            if line.startswith("diff --git "):
                file_count += 1
            elif line.startswith("+++") or line.startswith("---"):
                continue
            elif line.startswith("+"):
                additions += 1
            elif line.startswith("-"):
                deletions += 1
        response.headers["ETag"] = etag
        return {
            "is_repo": True,
            "diff": diff_text,
            "additions": additions,
            "deletions": deletions,
            "file_count": file_count,
            "untracked": untracked,
        }

    # -------------------------------------------------------------------
    # Input — text, keys, slash commands
    # -------------------------------------------------------------------

    @app.post("/api/sessions/{window_id}/text")
    async def send_text(
        window_id: str,
        req: SendTextRequest,
        _user: str = Depends(require_auth),
    ) -> dict[str, Any]:
        if not req.text and not req.enter:
            raise HTTPException(400, detail="empty text")
        if req.enter:
            ok, msg = await session_manager.send_to_window(window_id, req.text)
        else:
            ok = await tmux_manager.send_keys(
                window_id, req.text, enter=False, literal=True
            )
            msg = "ok" if ok else "send_keys failed"
        if not ok:
            raise HTTPException(400, detail=msg)
        return {"ok": True, "message": msg}

    @app.post("/api/sessions/{window_id}/keys")
    async def send_key(
        window_id: str,
        req: SendKeyRequest,
        _user: str = Depends(require_auth),
    ) -> dict[str, Any]:
        key = req.key
        if key not in ALLOWED_KEYS:
            raise HTTPException(400, detail=f"key not allowed: {key}")
        ok = await tmux_manager.send_keys(window_id, key, enter=False, literal=False)
        if not ok:
            raise HTTPException(400, detail="send_keys failed")
        return {"ok": True}

    @app.post("/api/sessions/{window_id}/command")
    async def send_command(
        window_id: str,
        req: SendCommandRequest,
        _user: str = Depends(require_auth),
    ) -> dict[str, Any]:
        cmd = req.command.strip()
        if not cmd:
            raise HTTPException(400, detail="empty command")
        if not cmd.startswith("/"):
            cmd = "/" + cmd
        ok, msg = await session_manager.send_to_window(window_id, cmd)
        if not ok:
            raise HTTPException(400, detail=msg)
        return {"ok": True, "message": msg}

    @app.get("/api/sessions/{window_id}/screenshot.png")
    async def screenshot(
        window_id: str, _user: str = Depends(require_auth)
    ) -> Response:
        png = await capture_screenshot(window_id)
        if png is None:
            raise HTTPException(404, detail="window not found or capture failed")
        return Response(content=png, media_type="image/png")

    @app.post("/api/sessions/{window_id}/upload")
    async def upload_image(
        window_id: str,
        request: Request,
        filename: str = Query("", max_length=200),
        _user: str = Depends(require_auth),
    ) -> dict[str, Any]:
        """Persist an image attachment and return its absolute path.

        The web composer uploads files here, then includes the returned path
        in the prompt (`(image attached: <path>)`) — same format the Telegram
        photo handler uses, so the agent reads files identically from both
        transports.
        """
        w = await tmux_manager.find_window_by_id(window_id)
        if not w:
            raise HTTPException(404, detail="window not found")

        body = await request.body()
        if not body:
            raise HTTPException(400, detail="empty body")
        if len(body) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, detail="file too large")

        ext = _safe_image_ext(filename) or _sniff_image_ext(body)
        if not ext:
            raise HTTPException(400, detail="unsupported image type")

        images_dir = codexbot_dir() / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        fname = f"{int(time.time())}_{secrets.token_hex(4)}{ext}"
        file_path = images_dir / fname
        file_path.write_bytes(body)
        return {"ok": True, "path": str(file_path)}

    # -------------------------------------------------------------------
    # Discovery — runtimes, skills, directories, resumable sessions
    # -------------------------------------------------------------------

    @app.get("/api/runtimes")
    async def list_runtimes_endpoint(
        _user: str = Depends(require_auth),
    ) -> dict[str, Any]:
        return {
            "runtimes": [
                {
                    "name": r.name,
                    "display_name": r.display_name,
                    "emoji": r.display_emoji,
                }
                for r in all_runtimes()
            ]
        }

    @app.get("/api/skills")
    async def skills(
        runtime: str = Query("codex"), _user: str = Depends(require_auth)
    ) -> dict[str, Any]:
        names = discover_skills(runtime)
        return {"skills": names, "runtime": runtime}

    # -------------------------------------------------------------------
    # Office viz state — the agent-visualization panel persists its custom
    # catalog and layout in a project-local JSON file so changes survive
    # page reloads and (if committed) live in version control. The file is
    # served via this API rather than as a static asset so writes don't have
    # to race with the Vite build pipeline. Schema:
    #   { "catalog": { id -> TileObject }, "layout": OfficeLayout | null }
    # -------------------------------------------------------------------

    _office_state_path = (
        Path(__file__).resolve().parents[3]
        / "web-ui"
        / "public"
        / "office"
        / "state.json"
    )

    @app.get("/api/office/state")
    async def get_office_state(
        _user: str = Depends(require_auth),
    ) -> dict[str, Any]:
        path = _office_state_path
        if not path.exists():
            return {"catalog": {}, "layout": None}
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            logger.warning("office state read failed: %s", exc)
            return {"catalog": {}, "layout": None}
        if not isinstance(data, dict):
            return {"catalog": {}, "layout": None}
        return {
            "catalog": data.get("catalog") or {},
            "layout": data.get("layout"),
        }

    @app.put("/api/office/state")
    async def put_office_state(
        body: dict[str, Any], _user: str = Depends(require_auth)
    ) -> dict[str, Any]:
        catalog = body.get("catalog")
        layout = body.get("layout")
        if catalog is not None and not isinstance(catalog, dict):
            raise HTTPException(400, detail="catalog must be an object")
        if layout is not None and not isinstance(layout, dict):
            raise HTTPException(400, detail="layout must be an object or null")
        payload = {"catalog": catalog or {}, "layout": layout}
        path = _office_state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        # Atomic write: write to tmp file then rename so a crashed write
        # can't leave a half-written JSON behind to confuse the next read.
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
        return {"ok": True, "path": str(path.relative_to(path.parents[3]))}

    @app.get("/api/directories")
    async def directories(
        path: str = Query("~"), _user: str = Depends(require_auth)
    ) -> dict[str, Any]:
        base = Path(path).expanduser()
        try:
            base = base.resolve(strict=True)
        except (FileNotFoundError, RuntimeError):
            raise HTTPException(404, detail="path not found")
        if not base.is_dir():
            raise HTTPException(400, detail="not a directory")

        show_hidden = config.show_hidden_dirs
        entries: list[dict[str, Any]] = []
        try:
            for child in sorted(base.iterdir(), key=lambda p: p.name.lower()):
                if not child.is_dir():
                    continue
                if not show_hidden and child.name.startswith("."):
                    continue
                entries.append({"name": child.name, "path": str(child)})
        except PermissionError:
            raise HTTPException(403, detail="permission denied")

        parent = None if base == base.parent else str(base.parent)
        return {"path": str(base), "parent": parent, "entries": entries}

    @app.get("/api/resume-sessions")
    async def resume_sessions(
        cwd: str = Query(...), _user: str = Depends(require_auth)
    ) -> dict[str, Any]:
        items = await session_manager.list_sessions_for_directory(cwd)
        return {
            "sessions": [
                {
                    "session_id": s.session_id,
                    "summary": s.summary,
                    "message_count": s.message_count,
                }
                for s in items
            ]
        }

    # -------------------------------------------------------------------
    # WebSocket: per-session terminal
    # -------------------------------------------------------------------

    @app.websocket("/api/sessions/{window_id}/term")
    async def terminal_socket(
        websocket: WebSocket, window_id: str, mode: str = Query("attach")
    ) -> None:
        # Same origin + cookie checks as the event stream.
        origin = websocket.headers.get("origin")
        host = websocket.headers.get("host", "")
        if not auth.origin_allowed(origin, request_host=host):
            await websocket.close(code=4403)
            return
        cookie = websocket.cookies.get(COOKIE_NAME)
        if not auth.verify_cookie(cookie):
            await websocket.close(code=4401)
            return
        if mode not in ("attach", "shell"):
            mode = "attach"
        window = await tmux_manager.find_window_by_id(window_id)
        if window is None:
            await websocket.close(code=4404)
            return

        await websocket.accept()

        # Build the child command. For "attach", create a per-client tmux
        # session grouped with the bot's session and target the requested
        # window — grouped sessions share the window set but maintain their
        # own active window pointer, so multiple web clients viewing
        # different topics don't fight over the bot's tmux clients.
        if mode == "attach":
            view_name = f"web-{window_id.lstrip('@')}-{secrets.token_hex(3)}"
            tmux_session = tmux_manager.session_name
            # Chain tmux commands via its own argv-level ';' separator —
            # safer than shell interpolation, regardless of session name.
            cmd = [
                "tmux",
                "new-session",
                "-d",
                "-s",
                view_name,
                "-t",
                tmux_session,
                ";",
                "select-window",
                "-t",
                f"{view_name}:{window_id}",
                ";",
                "attach-session",
                "-t",
                view_name,
            ]
            cleanup_session = view_name
        else:
            user_shell = os.environ.get("SHELL", "/bin/bash")
            cmd = [user_shell, "-i"]
            cleanup_session = None

        cwd = window.cwd or os.path.expanduser("~")

        # Spawn PTY child.
        import fcntl
        import pty
        import signal
        import struct
        import subprocess
        import termios

        pid, master_fd = pty.fork()
        if pid == 0:
            try:
                if cwd and os.path.isdir(cwd):
                    os.chdir(cwd)
                os.environ["TERM"] = "xterm-256color"
                os.execvp(cmd[0], cmd)
            except Exception:
                os._exit(1)

        # Non-blocking master fd.
        fl = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

        loop = asyncio.get_running_loop()
        out_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=256)

        def _on_pty_readable() -> None:
            try:
                data = os.read(master_fd, 8192)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                try:
                    out_queue.put_nowait(b"")
                except asyncio.QueueFull:
                    pass
                return
            if not data:
                try:
                    out_queue.put_nowait(b"")
                except asyncio.QueueFull:
                    pass
                return
            try:
                out_queue.put_nowait(data)
            except asyncio.QueueFull:
                # Drop oldest if the websocket is slow — terminals can
                # always recover from a missed chunk on the next redraw.
                try:
                    out_queue.get_nowait()
                    out_queue.put_nowait(data)
                except Exception:
                    pass

        loop.add_reader(master_fd, _on_pty_readable)

        async def pty_to_ws() -> None:
            while True:
                data = await out_queue.get()
                if data == b"":
                    return
                try:
                    await websocket.send_bytes(data)
                except Exception:
                    return

        async def ws_to_pty() -> None:
            while True:
                try:
                    msg = await websocket.receive()
                except WebSocketDisconnect:
                    return
                if msg.get("type") == "websocket.disconnect":
                    return
                if msg.get("bytes") is not None:
                    try:
                        os.write(master_fd, msg["bytes"])
                    except OSError:
                        return
                elif msg.get("text") is not None:
                    text = msg["text"]
                    ctrl: dict[str, Any] | None = None
                    try:
                        parsed = json.loads(text)
                        if isinstance(parsed, dict) and "type" in parsed:
                            ctrl = parsed
                    except Exception:
                        ctrl = None
                    if ctrl is None:
                        # Not a control frame — forward verbatim to the PTY.
                        try:
                            os.write(master_fd, text.encode("utf-8"))
                        except OSError:
                            return
                        continue
                    if ctrl.get("type") == "resize":
                        try:
                            rows = max(1, min(500, int(ctrl.get("rows", 24))))
                            cols = max(1, min(500, int(ctrl.get("cols", 80))))
                            fcntl.ioctl(
                                master_fd,
                                termios.TIOCSWINSZ,
                                struct.pack("HHHH", rows, cols, 0, 0),
                            )
                        except Exception:
                            pass

        pty_tasks = {
            asyncio.create_task(pty_to_ws(), name=f"term-pty-to-ws-{window_id}"),
            asyncio.create_task(ws_to_pty(), name=f"term-ws-to-pty-{window_id}"),
        }
        try:
            _done, pending = await asyncio.wait(
                pty_tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
        finally:
            for task in pty_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pty_tasks, return_exceptions=True)
            try:
                loop.remove_reader(master_fd)
            except Exception:
                pass
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            # Reap so we don't leave zombies.
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
            try:
                os.close(master_fd)
            except OSError:
                pass
            if cleanup_session:
                try:
                    subprocess.run(
                        ["tmux", "kill-session", "-t", cleanup_session],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=3,
                    )
                except Exception:
                    pass
            try:
                await websocket.close()
            except Exception:
                pass

    # -------------------------------------------------------------------
    # WebSocket event stream
    # -------------------------------------------------------------------

    @app.websocket("/api/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        # CSWSH defense: a cross-origin page in the same browser would
        # otherwise reach us with the cookie attached. Accept same-origin
        # handshakes plus any explicit allowlist entries.
        origin = websocket.headers.get("origin")
        host = websocket.headers.get("host", "")
        if not auth.origin_allowed(origin, request_host=host):
            logger.info("Rejected WS handshake: origin=%r host=%r", origin, host)
            await websocket.close(code=4403)
            return
        cookie = websocket.cookies.get(COOKIE_NAME)
        if not auth.verify_cookie(cookie):
            await websocket.close(code=4401)
            return
        await websocket.accept()
        queue = bus.subscribe()
        try:
            await websocket.send_json(
                {"type": "hello", "ts": asyncio.get_event_loop().time()}
            )
            while True:
                event = await queue.get()
                if event.get("type") == bus.SHUTDOWN_EVENT_TYPE:
                    break
                await websocket.send_json(event)
        except WebSocketDisconnect:
            pass
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.info("WebSocket disconnected with error: %s", exc)
        finally:
            bus.unsubscribe(queue)

    # -------------------------------------------------------------------
    # Static SPA hosting (built dist) + SPA fallback
    # -------------------------------------------------------------------

    # web-ui/dist relative to repo root: src/codexbot/web/api.py → repo/web-ui/dist
    repo_root = Path(__file__).resolve().parents[3]
    dist_dir = repo_root / "web-ui" / "dist"
    dist_override = os.getenv("CODEXBOT_WEB_DIST", "").strip()
    if dist_override:
        dist_dir = Path(dist_override).expanduser()

    if dist_dir.is_dir():
        # Serve hashed assets directly.
        app.mount(
            "/assets",
            StaticFiles(directory=str(dist_dir / "assets")),
            name="assets",
        )
        index_html = dist_dir / "index.html"

        @app.get("/", include_in_schema=False)
        @app.get("/{path:path}", include_in_schema=False)
        async def spa_fallback(path: str = "") -> Response:
            # Pass-through for files that exist outside /assets (favicon, etc.)
            candidate = dist_dir / path
            if path and candidate.is_file():
                return FileResponse(candidate)
            if index_html.is_file():
                return FileResponse(index_html)
            raise HTTPException(404, detail="not found")
    else:

        @app.get("/", include_in_schema=False)
        async def dev_placeholder() -> JSONResponse:
            return JSONResponse(
                {
                    "ok": True,
                    "message": (
                        "Web UI bundle not built. Run `pnpm install && pnpm build` "
                        "inside `web-ui/` or run the Vite dev server."
                    ),
                }
            )

    return app


__all__ = ["create_app", "ALLOWED_KEYS"]


# Re-export json/asyncio for tests that monkeypatch them.
_ = json
