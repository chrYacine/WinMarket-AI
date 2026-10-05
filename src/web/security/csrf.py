"""Double-submit-cookie CSRF protection.

Independent of the auth session, so it protects forms used by anonymous
visitors too (register, contact) as well as authenticated POSTs (logout,
account updates). A signed, unpredictable token is set as a cookie on any
page that renders a form; the form echoes it back as a hidden field; the
POST handler checks both match. Simple on purpose — no server-side token
store, no extra table.

Usage in a GET route that renders a form:

    token, is_new = get_or_create_csrf_token(request)
    resp = templates.TemplateResponse(request, "login.html", {"csrf_token": token, ...})
    if is_new:
        attach_csrf_cookie(resp, token)
    return resp

Reusing the existing cookie's token when it's still valid (rather than
minting a fresh one on every single GET) matters in practice: a user
often has more than one page open at once — two tabs, a background
browser prefetch/prerender, a page restored from history — and each of
those independently renders a form with an embedded token. If every
render also overwrote the shared cookie, whichever page was rendered
*last* would silently invalidate the token embedded in every other
already-open page, and submitting one of those would fail CSRF
validation with no obvious cause from the user's point of view.

(Cookies must be set directly on the Response object being returned —
FastAPI does not merge cookies from a separately injected `Response`
parameter when the endpoint returns a Response subclass such as
TemplateResponse or RedirectResponse.)
"""
from __future__ import annotations

import hmac
import secrets

from itsdangerous import BadSignature, URLSafeSerializer

from fastapi import HTTPException, Request, Response, status

from src.core import config

_SALT = "wm-csrf-v1"
_COOKIE_NAME = "wm_csrf"


def _serializer() -> URLSafeSerializer:
    if not config.SESSION_SECRET:
        raise RuntimeError("SESSION_SECRET n'est pas configuré. Renseigne-le dans .env.")
    return URLSafeSerializer(config.SESSION_SECRET, salt=_SALT)


def issue_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def get_or_create_csrf_token(request: Request) -> tuple[str, bool]:
    """Returns (token, is_new). Reuses the current cookie's token when it's
    still present and validly signed; only mints a new one otherwise (first
    visit, cleared cookies, or a corrupted/tampered value). The caller must
    call attach_csrf_cookie() when is_new is True."""
    raw = request.cookies.get(_COOKIE_NAME)
    if raw:
        try:
            return _serializer().loads(raw), False
        except BadSignature:
            pass
    return issue_csrf_token(), True


def attach_csrf_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=_COOKIE_NAME,
        value=_serializer().dumps(token),
        httponly=True,
        samesite="lax",
        secure=(config.APP_ENV == "production"),
        path="/",
    )


def verify_csrf(request: Request, submitted_token: str | None) -> bool:
    raw = request.cookies.get(_COOKIE_NAME)
    if not raw or not submitted_token:
        return False
    try:
        cookie_token = _serializer().loads(raw)
    except BadSignature:
        return False
    return hmac.compare_digest(cookie_token, submitted_token)


def require_csrf(request: Request, submitted_token: str | None) -> None:
    if not verify_csrf(request, submitted_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requête invalide (jeton de sécurité manquant ou expiré). Rechargez la page et réessayez.",
        )
