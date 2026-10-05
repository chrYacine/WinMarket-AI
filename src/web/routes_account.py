"""Account pages: /account, /account/pending, /forgot-password, /reset-password.

Password reset (B21-T1): request + consumption endpoints are rate-limited,
the token lookup is a fast indexed digest (no more Argon2 loop — see
src/web/database/repositories/password_reset_tokens.py), consumption is a
single atomic conditional UPDATE (no read-then-write race), a successful
reset bumps the user's session_version (revoking every other already-
issued cookie — see src/web/auth/session_cookie.py and
src/web/auth/dependencies.py::get_current_user) and invalidates every
other still-valid reset token for that user, and the reset link is
delivered through src/web/services/email_service.py using a trusted
config.BASE_URL origin — never the incoming request's Host header. See
docs/api/B21_T1_PASSWORD_RESET_CONTRACT.md for the full contract.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from src.core import config
from src.core.logger import get_agent_logger
from src.web.auth import service as auth_service
from src.web.auth.dependencies import get_current_user
from src.web.database.repositories import password_reset_tokens as reset_tokens_repo
from src.web.database.repositories import subscriptions as subscriptions_repo
from src.web.database.repositories import users as users_repo
from src.web.database.session import get_db
from src.web.security.csrf import attach_csrf_cookie, get_or_create_csrf_token, require_csrf
# B14-T1's generalized rate-limit primitive (built in parallel by Agent A)
# has landed with exactly the action-specific config constants this ticket
# needs: config.RATE_LIMIT_RESET_REQUEST_* / RATE_LIMIT_RESET_CONSUME_*
# (see src/core/config.py and docs/api/B14_T1_CSRF_RATE_LIMIT_CONTRACT.md).
from src.web.security.rate_limit import RateLimitExceeded, check_and_record
from src.web.services.email_service import send_password_reset_email
from src.web.templating import templates

router = APIRouter()
logger = get_agent_logger("web_account")

_RESET_TOKEN_TTL_SECONDS = 3600
_GENERIC_INVALID_TOKEN_MESSAGE = "Ce lien de réinitialisation est invalide ou a expiré."
_GENERIC_RATE_LIMITED_MESSAGE = "Trop de tentatives. Réessayez dans quelques instants."


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.get("/account", response_class=HTMLResponse)
def account_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login?next=/account", status_code=303)

    subscription = subscriptions_repo.get_latest_for_user(db, user.id)
    token, is_new = get_or_create_csrf_token(request)
    resp = templates.TemplateResponse(request, "account.html", {
        "user": user, "subscription": subscription, "csrf_token": token, "saved": False,
    })
    if is_new:
        attach_csrf_cookie(resp, token)
    return resp


@router.post("/account", response_class=HTMLResponse)
def account_update(
    request: Request,
    db: Session = Depends(get_db),
    first_name: str = Form(...),
    last_name: str = Form(...),
    company: str = Form(""),
    job_title: str = Form(""),
    phone: str = Form(""),
    csrf_token: str = Form(...),
):
    require_csrf(request, csrf_token)
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login?next=/account", status_code=303)

    user.first_name = first_name.strip() or user.first_name
    user.last_name = last_name.strip() or user.last_name
    user.company = company.strip() or None
    user.job_title = job_title.strip() or None
    user.phone = phone.strip() or None
    db.commit()

    subscription = subscriptions_repo.get_latest_for_user(db, user.id)
    token, is_new = get_or_create_csrf_token(request)
    resp = templates.TemplateResponse(request, "account.html", {
        "user": user, "subscription": subscription, "csrf_token": token, "saved": True,
    })
    if is_new:
        attach_csrf_cookie(resp, token)
    return resp


@router.get("/account/pending", response_class=HTMLResponse)
def account_pending_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    token, is_new = get_or_create_csrf_token(request)
    resp = templates.TemplateResponse(request, "account_pending.html", {"user": user, "csrf_token": token})
    if is_new:
        attach_csrf_cookie(resp, token)
    return resp


# ─── Password reset ─────────────────────────────────────────────────────────
# PASSWORD RESET STATUS (B21-T1): indexed digest lookup (no Argon2 loop),
# atomic single-use consumption, session_version revocation on success,
# sibling-token invalidation, rate-limited, and the reset email is now
# actually sent via src/web/services/email_service.py — using a trusted
# config.BASE_URL origin, never the incoming request's Host header.

@router.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_page(request: Request):
    token, is_new = get_or_create_csrf_token(request)
    resp = templates.TemplateResponse(request, "forgot_password.html", {"csrf_token": token, "submitted": False})
    if is_new:
        attach_csrf_cookie(resp, token)
    return resp


@router.post("/forgot-password", response_class=HTMLResponse)
def forgot_password_submit(request: Request, db: Session = Depends(get_db), email: str = Form(...), csrf_token: str = Form(...)):
    require_csrf(request, csrf_token)

    def render(submitted: bool, error: str | None = None):
        token, is_new = get_or_create_csrf_token(request)
        resp = templates.TemplateResponse(request, "forgot_password.html", {
            "csrf_token": token, "submitted": submitted, "error": error,
        })
        if is_new:
            attach_csrf_cookie(resp, token)
        return resp

    # Rate-limit key is ALWAYS the requester IP — never the target email —
    # so the limiter's own behavior can never be used to probe whether an
    # email exists (it triggers identically for a real or a fake address).
    try:
        check_and_record(
            "reset_request", _client_ip(request),
            max_attempts=config.RATE_LIMIT_RESET_REQUEST_MAX_ATTEMPTS,
            window_seconds=config.RATE_LIMIT_RESET_REQUEST_WINDOW_SECONDS,
        )
    except RateLimitExceeded:
        logger.warning("Password reset request rate-limited")
        return render(submitted=False, error=_GENERIC_RATE_LIMITED_MESSAGE)

    user = users_repo.get_by_email(db, email)
    if user is not None:
        raw_token = secrets.token_urlsafe(32)
        reset_tokens_repo.create_token(db, user_id=user.id, raw_token=raw_token, ttl_seconds=_RESET_TOKEN_TTL_SECONDS)
        db.commit()

        # Trusted origin ONLY — config.BASE_URL, never request.base_url /
        # the Host header, which an attacker can set arbitrarily on a
        # request that still legitimately reaches this server (host-header
        # poisoning into a password-reset link).
        reset_url = f"{config.BASE_URL.rstrip('/')}/reset-password?token={raw_token}"
        try:
            send_password_reset_email(user.email, reset_url)
        except Exception:
            # send_password_reset_email already never raises by contract,
            # but this belt-and-suspenders catch guarantees a delivery
            # failure can NEVER change this response or leak the token.
            logger.exception("Unexpected error while sending password reset email (recipient/token masked)")
        logger.info("Password reset token issued user_id=%s", user.id)
    # Always the same response — do not reveal whether the email exists.
    return render(submitted=True)


@router.get("/reset-password", response_class=HTMLResponse)
def reset_password_page(request: Request, token: str = ""):
    csrf, is_new = get_or_create_csrf_token(request)
    resp = templates.TemplateResponse(request, "reset_password.html", {
        "csrf_token": csrf, "reset_token": token, "error": None, "done": False,
    })
    if is_new:
        attach_csrf_cookie(resp, csrf)
    return resp


@router.post("/reset-password", response_class=HTMLResponse)
def reset_password_submit(
    request: Request,
    db: Session = Depends(get_db),
    reset_token: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    csrf_token: str = Form(...),
):
    require_csrf(request, csrf_token)

    def render_error(message: str):
        csrf, is_new = get_or_create_csrf_token(request)
        resp = templates.TemplateResponse(request, "reset_password.html", {
            "csrf_token": csrf, "reset_token": reset_token, "error": message, "done": False,
        })
        if is_new:
            attach_csrf_cookie(resp, csrf)
        return resp

    # Same IP-keyed rate limit as the request endpoint — applied before any
    # DB work, so brute-forcing candidate tokens is bounded too.
    try:
        check_and_record(
            "reset_consume", _client_ip(request),
            max_attempts=config.RATE_LIMIT_RESET_CONSUME_MAX_ATTEMPTS,
            window_seconds=config.RATE_LIMIT_RESET_CONSUME_WINDOW_SECONDS,
        )
    except RateLimitExceeded:
        logger.warning("Password reset consumption rate-limited")
        return render_error(_GENERIC_RATE_LIMITED_MESSAGE)

    if password != password_confirm:
        return render_error("Les mots de passe ne correspondent pas.")
    strength_error = auth_service.password_strength_error(password)
    if strength_error:
        return render_error(strength_error)

    now = datetime.now(timezone.utc)

    # Single atomic conditional UPDATE — no preceding SELECT decides the
    # outcome, so two concurrent submissions of the SAME token can never
    # both succeed (see repositories/password_reset_tokens.py::try_consume,
    # same idiom as analysis_jobs_repo.try_claim).
    consumed = reset_tokens_repo.try_consume(db, raw_token=reset_token, now=now)
    if not consumed:
        db.rollback()
        return render_error(_GENERIC_INVALID_TOKEN_MESSAGE)

    token_row = reset_tokens_repo.get_by_raw_token(db, raw_token=reset_token)
    user = users_repo.get_by_id(db, token_row.user_id) if token_row is not None else None
    if user is None:
        # Should not happen (FK-guaranteed), but never leave a token
        # consumed with nothing to show for it, and never leak state.
        db.rollback()
        return render_error(_GENERIC_INVALID_TOKEN_MESSAGE)

    # All three — token consumption (already applied above, same
    # transaction), password change, and session revocation — commit or
    # roll back together as one unit: this request's `db` session's single
    # transaction is that unit, committed once at the end.
    user.password_hash = auth_service.hash_password(password)
    user.session_version = (user.session_version or 0) + 1

    # Any OTHER still-valid token for this same user must stop working too
    # (a user who requested reset twice and used the second link should
    # not have the first link still work).
    reset_tokens_repo.invalidate_other_tokens_for_user(db, user_id=user.id, exclude_id=token_row.id, now=now)

    db.commit()

    csrf, is_new = get_or_create_csrf_token(request)
    resp = templates.TemplateResponse(request, "reset_password.html", {
        "csrf_token": csrf, "reset_token": "", "error": None, "done": True,
    })
    if is_new:
        attach_csrf_cookie(resp, csrf)
    return resp
