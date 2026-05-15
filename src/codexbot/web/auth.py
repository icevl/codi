"""Password + TOTP authentication for the web UI.

Strategy: the server is configured with a shared `WEB_UI_PASSWORD` and a
per-install TOTP secret (Google Authenticator-compatible). A successful login
mints a signed cookie (via `itsdangerous.TimestampSigner`) that is verified on
every subsequent request. WebSocket connections present the cookie via the
standard browser cookie header during the upgrade.

The signing key is taken from `config.web_ui_secret` and persists between
restarts (regenerating it would simply log everyone out). The TOTP secret is
persisted at `~/.codexbot/web_ui_totp_secret` (mode 0600) or overridden via
`WEB_UI_TOTP_SECRET`.
"""

from __future__ import annotations

import hmac
import io
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Iterable

import pyotp
import qrcode
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

logger = logging.getLogger(__name__)

COOKIE_NAME = "codexbot_session"
COOKIE_MAX_AGE_SECONDS = 30 * 24 * 3600  # 30 days
SESSION_SALT = "codexbot.web.session"
# How many 30-second windows on either side of "now" we accept. 1 ≈ ±30s of
# clock skew, which is what most TOTP implementations allow.
TOTP_VALID_WINDOW = 1


@dataclass(frozen=True)
class AuthConfig:
    password: str
    secret: str
    # TOTP — empty string means 2FA is disabled (legacy single-factor mode).
    totp_secret: str = ""
    totp_issuer: str = "CodexBot"
    totp_account: str = "web"
    # "auto" | "true" | "false" — see config.web_ui_cookie_secure.
    cookie_secure_mode: str = "auto"
    # Allowlisted Origin values for the WebSocket upgrade.
    allowed_origins: tuple[str, ...] = field(default_factory=tuple)

    @property
    def enabled(self) -> bool:
        return bool(self.password)

    @property
    def totp_enabled(self) -> bool:
        return bool(self.totp_secret)


class Authenticator:
    """Verify passwords / TOTP codes and mint/verify session cookies."""

    def __init__(self, cfg: AuthConfig) -> None:
        self._cfg = cfg
        self._signer = TimestampSigner(cfg.secret, salt=SESSION_SALT)
        self._totp = pyotp.TOTP(cfg.totp_secret) if cfg.totp_secret else None
        # Replay protection: store the highest TOTP counter we've already
        # accepted. A code can only be used once even within its 30-second
        # window. Cross-thread access from FastAPI's threadpool is possible
        # for sync routes, hence the lock.
        self._totp_used_counter: int = -1
        self._totp_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Capability flags
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._cfg.enabled

    @property
    def totp_enabled(self) -> bool:
        return self._cfg.totp_enabled

    @property
    def cookie_secure_mode(self) -> str:
        return self._cfg.cookie_secure_mode

    @property
    def allowed_origins(self) -> tuple[str, ...]:
        return self._cfg.allowed_origins

    # ------------------------------------------------------------------
    # Password
    # ------------------------------------------------------------------

    def check_password(self, supplied: str) -> bool:
        if not self._cfg.enabled:
            return False
        return hmac.compare_digest(
            (supplied or "").encode("utf-8"),
            self._cfg.password.encode("utf-8"),
        )

    # ------------------------------------------------------------------
    # TOTP
    # ------------------------------------------------------------------

    def check_totp(self, code: str) -> bool:
        """Verify a 6-digit TOTP code with replay protection.

        We accept the current 30-second window plus one window on either
        side (±30s skew). Each accepted counter is recorded so the same
        code cannot be replayed within its validity window.
        """
        if self._totp is None:
            # Server doesn't require TOTP — accept any (empty) code.
            return True
        cleaned = (code or "").strip().replace(" ", "")
        if not cleaned.isdigit() or len(cleaned) != 6:
            return False
        now = int(time.time())
        step = self._totp.interval
        with self._totp_lock:
            # Walk the accepted window from oldest to newest so a replay
            # attempt within the same window is rejected even if the user
            # also submitted a fresh, valid code seconds later.
            for offset in range(-TOTP_VALID_WINDOW, TOTP_VALID_WINDOW + 1):
                counter = now // step + offset
                if counter <= self._totp_used_counter:
                    continue
                expected = self._totp.at(now + offset * step)
                if hmac.compare_digest(expected, cleaned):
                    self._totp_used_counter = counter
                    return True
            return False

    def provisioning_uri(self) -> str | None:
        """Return the `otpauth://` URI to feed Google Authenticator."""
        if self._totp is None:
            return None
        return self._totp.provisioning_uri(
            name=self._cfg.totp_account, issuer_name=self._cfg.totp_issuer
        )

    def enrollment_qr_ascii(self) -> str | None:
        """Render the provisioning URI as ASCII so it can be logged."""
        uri = self.provisioning_uri()
        if uri is None:
            return None
        qr = qrcode.QRCode(border=1)
        qr.add_data(uri)
        qr.make(fit=True)
        buf = io.StringIO()
        qr.print_ascii(out=buf, invert=True)
        return buf.getvalue()

    # ------------------------------------------------------------------
    # Cookie
    # ------------------------------------------------------------------

    def mint_cookie(self, subject: str = "user") -> str:
        token = self._signer.sign(subject.encode("utf-8"))
        return token.decode("utf-8")

    def verify_cookie(self, raw: str | None) -> str | None:
        if not raw:
            return None
        try:
            value = self._signer.unsign(raw, max_age=COOKIE_MAX_AGE_SECONDS)
        except SignatureExpired:
            logger.info("Rejected expired session cookie")
            return None
        except BadSignature:
            logger.info("Rejected malformed session cookie")
            return None
        return value.decode("utf-8")

    # ------------------------------------------------------------------
    # Origin allowlist (WebSocket CSRF / CSWSH)
    # ------------------------------------------------------------------

    def origin_allowed(self, origin: str | None, *, request_host: str = "") -> bool:
        """Reject WebSocket upgrades from unexpected origins.

        Accepts the request when:
          * Origin equals the request's own Host (same-origin) — covers any
            address the user actually reaches the server through (loopback,
            Tailscale, LAN IP, hostname), without needing to enumerate them
            up front.
          * Origin is in the explicit `WEB_UI_ALLOWED_ORIGINS` list — used
            when fronting with a reverse proxy that rewrites Host.

        A missing Origin is rejected — every modern browser sets it on the
        WS handshake; absence usually means a non-browser caller, which the
        web UI doesn't expose.
        """
        if not origin:
            return False
        if request_host:
            for scheme in ("http://", "https://"):
                if origin == f"{scheme}{request_host}":
                    return True
        return origin in self._cfg.allowed_origins


def resolve_cookie_secure(mode: str, request_is_https: bool) -> bool:
    """Resolve the Set-Cookie `Secure` attribute from config + request."""
    if mode == "true":
        return True
    if mode == "false":
        return False
    return request_is_https


def set_session_cookie(response: object, value: str, *, secure: bool) -> None:
    """Apply the session cookie attributes to a Starlette/FastAPI response."""
    setter = getattr(response, "set_cookie", None)
    if setter is None:
        raise TypeError("response does not support set_cookie")
    setter(
        key=COOKIE_NAME,
        value=value,
        max_age=COOKIE_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


# Re-export `time` only to make tests' monkeypatch points obvious.
_ = (time, Iterable)
