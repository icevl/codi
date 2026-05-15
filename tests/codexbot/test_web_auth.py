"""Unit tests for the web UI authenticator."""

import pyotp

from codexbot.web.auth import AuthConfig, Authenticator, resolve_cookie_secure


def _auth(
    password: str = "hunter2",
    secret: str = "test-secret",
    totp_secret: str = "",
    allowed_origins: tuple[str, ...] = (),
) -> Authenticator:
    return Authenticator(
        AuthConfig(
            password=password,
            secret=secret,
            totp_secret=totp_secret,
            allowed_origins=allowed_origins,
        )
    )


def test_disabled_when_no_password() -> None:
    auth = _auth(password="")
    assert auth.enabled is False
    assert auth.check_password("anything") is False


def test_check_password_constant_time_match() -> None:
    auth = _auth(password="open-sesame")
    assert auth.check_password("open-sesame") is True
    assert auth.check_password("Open-Sesame") is False
    assert auth.check_password("") is False


def test_round_trip_cookie() -> None:
    auth = _auth()
    cookie = auth.mint_cookie("admin")
    assert auth.verify_cookie(cookie) == "admin"


def test_cookie_with_different_secret_rejected() -> None:
    cookie = _auth(secret="secret-a").mint_cookie("admin")
    assert _auth(secret="secret-b").verify_cookie(cookie) is None


def test_garbage_cookie_rejected() -> None:
    auth = _auth()
    assert auth.verify_cookie(None) is None
    assert auth.verify_cookie("") is None
    assert auth.verify_cookie("not.a.signed.cookie") is None


# ---------------------------------------------------------------------------
# TOTP
# ---------------------------------------------------------------------------


def test_totp_disabled_passes_through() -> None:
    auth = _auth()
    assert auth.totp_enabled is False
    # When 2FA isn't configured the check is a no-op pass.
    assert auth.check_totp("") is True
    assert auth.check_totp("123456") is True


def test_totp_accepts_current_code() -> None:
    secret = pyotp.random_base32()
    auth = _auth(totp_secret=secret)
    assert auth.totp_enabled is True
    code = pyotp.TOTP(secret).now()
    assert auth.check_totp(code) is True


def test_totp_rejects_wrong_code() -> None:
    secret = pyotp.random_base32()
    auth = _auth(totp_secret=secret)
    assert auth.check_totp("000000") is False
    assert auth.check_totp("abcdef") is False
    assert auth.check_totp("") is False


def test_totp_rejects_replay() -> None:
    """A code is single-use within its 30-second window."""
    secret = pyotp.random_base32()
    auth = _auth(totp_secret=secret)
    code = pyotp.TOTP(secret).now()
    assert auth.check_totp(code) is True
    # Same code, second submission — must be rejected.
    assert auth.check_totp(code) is False


def test_totp_accepts_spaces_and_strips() -> None:
    secret = pyotp.random_base32()
    auth = _auth(totp_secret=secret)
    code = pyotp.TOTP(secret).now()
    spaced = f"{code[:3]} {code[3:]}"
    assert auth.check_totp(spaced) is True


def test_provisioning_uri_and_qr() -> None:
    secret = pyotp.random_base32()
    auth = _auth(totp_secret=secret)
    uri = auth.provisioning_uri()
    assert uri is not None and uri.startswith("otpauth://totp/")
    qr = auth.enrollment_qr_ascii()
    assert qr is not None and "\n" in qr


# ---------------------------------------------------------------------------
# Origin allowlist
# ---------------------------------------------------------------------------


def test_origin_allowlist_match() -> None:
    auth = _auth(allowed_origins=("http://127.0.0.1:8787", "https://example.com"))
    assert auth.origin_allowed("http://127.0.0.1:8787") is True
    assert auth.origin_allowed("https://example.com") is True
    assert auth.origin_allowed("https://evil.com") is False
    assert auth.origin_allowed("") is False
    assert auth.origin_allowed(None) is False


def test_origin_same_origin_match() -> None:
    """An address not in the allowlist still works when it matches the Host."""
    auth = _auth(allowed_origins=())
    # Tailscale IP, LAN IP, hostname — all accepted as long as Origin and
    # Host match.
    assert (
        auth.origin_allowed(
            "http://100.64.1.13:8787", request_host="100.64.1.13:8787"
        )
        is True
    )
    assert (
        auth.origin_allowed(
            "https://my-laptop.local:8787", request_host="my-laptop.local:8787"
        )
        is True
    )
    # Mismatched host -> rejected.
    assert (
        auth.origin_allowed(
            "https://evil.com", request_host="my-laptop.local:8787"
        )
        is False
    )


# ---------------------------------------------------------------------------
# Cookie secure resolution
# ---------------------------------------------------------------------------


def test_resolve_cookie_secure_modes() -> None:
    assert resolve_cookie_secure("auto", request_is_https=True) is True
    assert resolve_cookie_secure("auto", request_is_https=False) is False
    assert resolve_cookie_secure("true", request_is_https=False) is True
    assert resolve_cookie_secure("false", request_is_https=True) is False
