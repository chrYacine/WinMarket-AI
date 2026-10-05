"""Signed, HttpOnly session cookie.

The cookie carries `user_id` PLUS a `session_version` "security stamp"
(B21-T1) — both signed and timestamped together (itsdangerous, keyed by
SESSION_SECRET). Every protected access re-reads the user (and their
subscription) from PostgreSQL, so a user disabled in the database is
blocked immediately even if their cookie is still valid — the cookie is
just a pointer, never the source of truth.

`session_version` is what makes password-reset revocation real: bumping
`User.session_version` (done atomically with a password change — see
src/web/routes_account.py::reset_password_submit) makes every cookie
minted before that bump carry a now-stale version, and
src/web/auth/dependencies.py::get_current_user rejects any cookie whose
embedded version doesn't match the user's CURRENT database value — the
exact same treatment as an invalid signature. Login (src/web/
routes_auth.py) must pass the user's CURRENT session_version when minting
a cookie: `set_user_session(resp, user.id, user.session_version)`.

Payload shape: `{"uid": str(user_id), "v": int(session_version)}`,
serialized via the same URLSafeTimedSerializer as before. A cookie minted
before this shape existed (a bare `dumps(str(user_id))` string, not a
dict) fails the shape check below and is treated as invalid — the
deliberate "old-shaped cookie = invalid, must log in again" choice
documented in docs/api/B21_T1_PASSWORD_RESET_CONTRACT.md and migration
0008's docstring.
"""
from __future__ import annotations

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from fastapi import Request, Response

from src.core import config

_SALT = "wm-auth-session-v1"


def _serializer() -> URLSafeTimedSerializer:
    if not config.SESSION_SECRET:
        raise RuntimeError(
            "SESSION_SECRET n'est pas configuré. Renseigne-le dans .env (voir .env.example)."
        )
    return URLSafeTimedSerializer(config.SESSION_SECRET, salt=_SALT)


def set_user_session(response: Response, user_id, session_version: int = 0) -> None:
    payload = {"uid": str(user_id), "v": int(session_version)}
    token = _serializer().dumps(payload)
    response.set_cookie(
        key=config.SESSION_COOKIE_NAME,
        value=token,
        max_age=config.SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=(config.APP_ENV == "production"),
        path="/",
    )


def get_session_identity(request: Request) -> tuple[str, int] | None:
    """Returns (user_id_str, session_version) for a validly-signed,
    correctly-shaped cookie, or None for anonymous/invalid/old-shaped
    cookies. This is the version-aware read every access-control path that
    must honor revocation (src/web/auth/dependencies.py::get_current_user)
    should use — see this module's docstring."""
    raw = request.cookies.get(config.SESSION_COOKIE_NAME)
    if not raw:
        return None
    try:
        payload = _serializer().loads(raw, max_age=config.SESSION_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(payload, dict) or "uid" not in payload or "v" not in payload:
        # Old-shaped (pre-B21-T1, plain-string) or otherwise malformed
        # payload — deliberately treated as invalid, not as version 0.
        return None
    try:
        return str(payload["uid"]), int(payload["v"])
    except (TypeError, ValueError):
        return None


def get_session_user_id(request: Request) -> str | None:
    """Back-compat convenience for any caller that only needs the user id
    and does not itself enforce revocation. Prefer get_session_identity
    for any access-control path — see src/web/auth/dependencies.py::
    get_current_user, which is the one path in this codebase that must
    enforce session_version and therefore does NOT use this function."""
    identity = get_session_identity(request)
    return identity[0] if identity else None


def clear_user_session(response: Response) -> None:
    response.delete_cookie(config.SESSION_COOKIE_NAME, path="/")
