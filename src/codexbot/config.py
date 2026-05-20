"""Application configuration loaded from environment variables.

Loads Telegram credentials, tmux settings, Codex transcript paths, and monitor
settings from `.env`/environment and exposes a singleton `config`.
"""

import logging
import os
import re
import shlex
from pathlib import Path

from dotenv import load_dotenv

from .utils import codexbot_dir

logger = logging.getLogger(__name__)

# Env vars that must not leak into spawned tmux child processes.
#
# OPENAI_BASE_URL is intentionally scrubbed so Codex CLI in tmux keeps its
# default auth/backend behavior (ChatGPT login). The bot still reads and stores
# it in config.openai_base_url for voice transcription requests.
SENSITIVE_ENV_VARS = {
    "TELEGRAM_BOT_TOKEN",
    "ALLOWED_USERS",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "WEB_UI_PASSWORD",
    "WEB_UI_SECRET",
    "WEB_UI_TOTP_SECRET",
}
FALSE_ENV_VALUES = {"0", "false", "no", "off"}
ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
OLD_DANGEROUS_FLAG = "--dangerously-skip-permissions"
CURRENT_DANGEROUS_FLAG = "--dangerously-bypass-approvals-and-sandbox"
CLAUDE_DANGEROUS_FLAG = "--dangerously-skip-permissions"


def _env_flag_enabled(var_name: str, default: bool = True) -> bool:
    raw = os.getenv(var_name)
    if raw is None:
        return default
    return raw.strip().lower() not in FALSE_ENV_VALUES


def _build_codex_command(base_command: str, auto_approve_dangerous: bool) -> str:
    try:
        tokens = shlex.split(base_command)
    except ValueError:
        return base_command
    if not tokens:
        return base_command

    command_idx = 0
    while command_idx < len(tokens) and ENV_ASSIGNMENT_RE.match(tokens[command_idx]):
        command_idx += 1
    if command_idx >= len(tokens):
        return base_command

    command_name = Path(tokens[command_idx]).name
    if command_name != "codex":
        return base_command

    has_old_flag = OLD_DANGEROUS_FLAG in tokens
    normalized_tokens = [token for token in tokens if token != OLD_DANGEROUS_FLAG]
    should_enable_dangerous = (
        auto_approve_dangerous
        or has_old_flag
        or CURRENT_DANGEROUS_FLAG in normalized_tokens
    )
    if should_enable_dangerous and CURRENT_DANGEROUS_FLAG not in normalized_tokens:
        normalized_tokens.append(CURRENT_DANGEROUS_FLAG)
    if normalized_tokens == tokens:
        return base_command
    return shlex.join(normalized_tokens)


def _build_claude_command(base_command: str) -> str:
    """Normalize the Claude Code launch command.

    The `--dangerously-skip-permissions` flag is not appended here — the
    runtime layer adds it based on `CLAUDEBOT_AUTO_APPROVE_DANGEROUS` so
    the env override can flip behavior without rewriting the command.
    """
    try:
        tokens = shlex.split(base_command)
    except ValueError:
        return base_command
    if not tokens:
        return base_command
    return shlex.join(tokens)


class Config:
    """Application configuration loaded from environment variables."""

    def __init__(self) -> None:
        self.config_dir = codexbot_dir()
        self.config_dir.mkdir(parents=True, exist_ok=True)

        # Load .env: local (cwd) takes priority over config_dir.
        local_env = Path(".env")
        global_env = self.config_dir / ".env"
        if local_env.is_file():
            load_dotenv(local_env)
            logger.debug("Loaded env from %s", local_env.resolve())
        if global_env.is_file():
            load_dotenv(global_env)
            logger.debug("Loaded env from %s", global_env)

        self.telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN") or ""
        if not self.telegram_bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN environment variable is required")

        # Web UI transport (optional). When `WEB_UI_PASSWORD` is set, an HTTP +
        # WebSocket server runs alongside the Telegram bot and exposes the same
        # session operations through a browser UI.
        self.web_ui_password: str = os.getenv("WEB_UI_PASSWORD", "").strip()
        self.web_ui_enabled: bool = bool(self.web_ui_password)
        # Auto-update checker (polls GitHub main every 10 min and shows a
        # banner in the web UI when there's a new commit). Defaults on;
        # set CODEXBOT_AUTO_UPDATE=false to disable the poll loop and
        # suppress the banner entirely.
        self.auto_update_enabled: bool = os.getenv(
            "CODEXBOT_AUTO_UPDATE", "true"
        ).strip().lower() not in ("false", "0", "no", "off")
        self.web_ui_host: str = (
            os.getenv("WEB_UI_HOST", "127.0.0.1").strip() or "127.0.0.1"
        )
        try:
            self.web_ui_port: int = int(os.getenv("WEB_UI_PORT", "8787"))
        except ValueError:
            self.web_ui_port = 8787
        # Secret used to sign session cookies. Persists across restarts.
        secret_file = self.config_dir / "web_ui_secret"
        env_secret = os.getenv("WEB_UI_SECRET", "").strip()
        if env_secret:
            self.web_ui_secret = env_secret
        elif secret_file.exists():
            self.web_ui_secret = secret_file.read_text().strip()
        else:
            import secrets

            self.web_ui_secret = secrets.token_urlsafe(48)
            try:
                secret_file.write_text(self.web_ui_secret)
                secret_file.chmod(0o600)
            except OSError as exc:
                logger.warning("Failed to persist web UI secret: %s", exc)

        # Two-factor auth (Google Authenticator / TOTP). Required by default
        # when the web UI is enabled — set WEB_UI_TOTP_REQUIRED=false to
        # disable, e.g. when running behind another auth layer like a VPN.
        self.web_ui_totp_required: bool = (
            _env_flag_enabled("WEB_UI_TOTP_REQUIRED", default=True)
            and self.web_ui_enabled
        )
        env_totp = os.getenv("WEB_UI_TOTP_SECRET", "").strip()
        totp_file = self.config_dir / "web_ui_totp_secret"
        self.web_ui_totp_secret_freshly_generated = False
        if env_totp:
            self.web_ui_totp_secret = env_totp
        elif totp_file.exists():
            self.web_ui_totp_secret = totp_file.read_text().strip()
        elif self.web_ui_totp_required:
            import pyotp

            self.web_ui_totp_secret = pyotp.random_base32()
            self.web_ui_totp_secret_freshly_generated = True
            try:
                totp_file.write_text(self.web_ui_totp_secret)
                totp_file.chmod(0o600)
            except OSError as exc:
                logger.warning("Failed to persist web UI TOTP secret: %s", exc)
        else:
            self.web_ui_totp_secret = ""

        # Issuer/account labels shown inside the authenticator app.
        self.web_ui_totp_issuer: str = (
            os.getenv("WEB_UI_TOTP_ISSUER", "").strip() or "CodexBot"
        )
        import socket as _socket

        self.web_ui_totp_account: str = (
            os.getenv("WEB_UI_TOTP_ACCOUNT", "").strip()
            or f"web@{_socket.gethostname()}"
        )

        # Cookie `secure` flag. `auto` (default) trusts the request scheme so
        # local HTTP keeps working while a reverse-proxied HTTPS deployment
        # gets the Secure flag. `true`/`false` force the value.
        cookie_mode = os.getenv("WEB_UI_COOKIE_SECURE", "auto").strip().lower()
        if cookie_mode not in {"auto", "true", "false"}:
            cookie_mode = "auto"
        self.web_ui_cookie_secure: str = cookie_mode

        # WebSocket Origin allowlist — `host:port` pairs, comma-separated.
        # The default covers loopback variants of the configured host/port so
        # the browser bundle works out of the box; reverse-proxy deployments
        # should set WEB_UI_ALLOWED_ORIGINS explicitly.
        origins_env = os.getenv("WEB_UI_ALLOWED_ORIGINS", "").strip()
        if origins_env:
            self.web_ui_allowed_origins: tuple[str, ...] = tuple(
                o.strip() for o in origins_env.split(",") if o.strip()
            )
        else:
            host = self.web_ui_host
            port = self.web_ui_port
            self.web_ui_allowed_origins = tuple(
                {
                    f"http://{host}:{port}",
                    f"https://{host}:{port}",
                    f"http://127.0.0.1:{port}",
                    f"http://localhost:{port}",
                }
            )

        allowed_users_str = os.getenv("ALLOWED_USERS", "")
        if not allowed_users_str:
            raise ValueError("ALLOWED_USERS environment variable is required")
        try:
            self.allowed_users: set[int] = {
                int(uid.strip()) for uid in allowed_users_str.split(",") if uid.strip()
            }
        except ValueError as e:
            raise ValueError(
                f"ALLOWED_USERS contains non-numeric value: {e}. "
                "Expected comma-separated Telegram user IDs."
            ) from e

        # Tmux session name and window naming.
        self.tmux_session_name = os.getenv("TMUX_SESSION_NAME", "codexbot")
        self.tmux_main_window_name = "__main__"

        # Codex command to run in new windows.
        base_codex_command = os.getenv("CODEX_COMMAND", "codex --no-alt-screen")
        self.auto_approve_dangerous = _env_flag_enabled(
            "CODEXBOT_AUTO_APPROVE_DANGEROUS", default=True
        )
        self.codex_command = _build_codex_command(
            base_codex_command,
            self.auto_approve_dangerous,
        )

        # Claude Code command to run in new windows.
        base_claude_command = os.getenv("CLAUDE_COMMAND", "claude")
        self.claude_command = _build_claude_command(base_claude_command)
        self.claude_auto_approve_dangerous = _env_flag_enabled(
            "CLAUDEBOT_AUTO_APPROVE_DANGEROUS", default=True
        )

        # State files under config_dir.
        self.state_file = self.config_dir / "state.json"
        self.monitor_state_file = self.config_dir / "monitor_state.json"

        # Codex transcript location.
        # Priority: CODEXBOT_CODEX_SESSIONS_PATH > CODEX_HOME/sessions > ~/.codex/sessions
        codex_sessions_env = os.getenv("CODEXBOT_CODEX_SESSIONS_PATH", "").strip()
        codex_home_env = os.getenv("CODEX_HOME", "").strip()
        default_sessions_dir = (
            Path(codex_home_env).expanduser() / "sessions"
            if codex_home_env
            else Path.home() / ".codex" / "sessions"
        )
        self.codex_sessions_path = (
            Path(codex_sessions_env).expanduser()
            if codex_sessions_env
            else default_sessions_dir
        )

        # Claude Code per-process state files at ~/.claude/sessions/<pid>.json
        # and JSONL transcripts under ~/.claude/projects/<encoded-cwd>/.
        claude_sessions_env = os.getenv("CLAUDEBOT_CLAUDE_SESSIONS_PATH", "").strip()
        claude_projects_env = os.getenv("CLAUDEBOT_CLAUDE_PROJECTS_PATH", "").strip()
        self.claude_sessions_path = (
            Path(claude_sessions_env).expanduser()
            if claude_sessions_env
            else Path.home() / ".claude" / "sessions"
        )
        self.claude_projects_path = (
            Path(claude_projects_env).expanduser()
            if claude_projects_env
            else Path.home() / ".claude" / "projects"
        )
        self.claude_session_detect_timeout = float(
            os.getenv("CLAUDEBOT_SESSION_DETECT_TIMEOUT", "15.0")
        )
        self.claude_session_detect_interval = float(
            os.getenv("CLAUDEBOT_SESSION_DETECT_INTERVAL", "0.5")
        )

        self.monitor_poll_interval = float(os.getenv("MONITOR_POLL_INTERVAL", "2.0"))
        log_level = os.getenv("CODEXBOT_LOG_LEVEL", "INFO").strip().upper()
        if log_level not in logging._nameToLevel:
            log_level = "INFO"
        self.log_level = log_level
        self.status_poll_interval = float(
            os.getenv("CODEXBOT_STATUS_POLL_INTERVAL", "1.0")
        )
        self.topic_check_interval = float(
            os.getenv("CODEXBOT_TOPIC_CHECK_INTERVAL", "60.0")
        )
        self.queue_maxsize = max(1, int(os.getenv("CODEXBOT_QUEUE_MAXSIZE", "500")))
        self.queue_drain_timeout_seconds = max(
            0.1, float(os.getenv("CODEXBOT_QUEUE_DRAIN_TIMEOUT_SECONDS", "5.0"))
        )
        self.history_cache_max_sessions = max(
            1, int(os.getenv("CODEXBOT_HISTORY_CACHE_MAX_SESSIONS", "8"))
        )
        self.session_detect_timeout = float(
            os.getenv("CODEXBOT_SESSION_DETECT_TIMEOUT", "12.0")
        )
        self.session_detect_interval = float(
            os.getenv("CODEXBOT_SESSION_DETECT_INTERVAL", "1.5")
        )
        self.session_stale_after_seconds = float(
            os.getenv("CODEXBOT_SESSION_STALE_AFTER_SECONDS", "90")
        )
        self.status_probe_min_interval_seconds = float(
            os.getenv("CODEXBOT_STATUS_PROBE_MIN_INTERVAL_SECONDS", "15")
        )
        self.monitor_new_session_tail_bytes = int(
            os.getenv("CODEXBOT_MONITOR_NEW_SESSION_TAIL_BYTES", "65536")
        )

        # Display user messages in history and real-time notifications.
        self.show_user_messages = True

        # Show hidden (dot) directories in directory browser.
        self.show_hidden_dirs = (
            os.getenv("CODEXBOT_SHOW_HIDDEN_DIRS", "").lower() == "true"
        )

        # OpenAI API for optional voice message transcription.
        self.openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
        self.openai_base_url: str = os.getenv(
            "OPENAI_BASE_URL", "https://api.openai.com/v1"
        )

        for var in SENSITIVE_ENV_VARS:
            os.environ.pop(var, None)

        logger.debug(
            "Config initialized: dir=%s, token=%s..., allowed_users=%d, "
            "tmux_session=%s, codex_sessions_path=%s",
            self.config_dir,
            self.telegram_bot_token[:8],
            len(self.allowed_users),
            self.tmux_session_name,
            self.codex_sessions_path,
        )

    def is_user_allowed(self, user_id: int) -> bool:
        """Check whether a user is in the allow list."""
        return user_id in self.allowed_users


config = Config()
