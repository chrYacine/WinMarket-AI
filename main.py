"""WinMarket AI — FastAPI web interface.

HTML/CSS/JS + FastAPI front door for WinMarket AI — its only interface (the
former Streamlit demo was removed in lot 43). It is a thin presentation
layer: every route delegates to the modules under src/agents, src/rag,
src/livrables and src/core — no business rule is reimplemented here.

Run with:
    uvicorn main:app --reload --port 8000
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from src.core import config
from src.web.body_limit_middleware import BodySizeLimitMiddleware
from src.web.routes_account import router as account_router
from src.web.routes_api import router as api_router
from src.web.routes_auth import router as auth_router
from src.web.routes_knowledge_documents import router as knowledge_documents_router
from src.web.routes_pages import router as pages_router
from src.web.routes_scoring_policy import router as scoring_policy_router
from fastapi.staticfiles import StaticFiles
from src.web.templating import templates


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # B24-T1: validate_config() is no longer called at bare import time
    # (see src/core/config.py's own comment for the confirmed defect this
    # fixes) — this is the "démarrage approprié" it now runs at instead:
    # once, before this process starts accepting traffic. Missing external
    # credentials only warn (the app's own supported no-key fallback still
    # works); a genuine internal coherence bug still raises and stops
    # startup, exactly as before.
    config.validate_config()

    # B12-T1: any analysis_jobs row still 'queued'/'running' from a
    # previous process cannot possibly still be in flight — the in-memory
    # queue.Queue that would have held it does not survive a restart. Marks
    # those 'interrupted' before this process starts accepting traffic, so
    # a user polling an old job_id gets an honest terminal state instead of
    # an eternal "running". No-op if no database is configured. See
    # src/web/job_executor.py::reconcile_on_startup.
    from src.web import job_executor
    job_executor.reconcile_on_startup()

    # Lot 50 bis §4: real expiration of abandoned dossier-preview staging rows, even if nobody ever revisits
    # them — see src/web/ao_dossier/expiry_sweeper.py for the safety reasoning (bounded batches, confirmed/
    # validated dossiers never touched, storage removed only after the DB row is committed deleted).
    from src.web.ao_dossier import expiry_sweeper
    expiry_sweeper.start_background_sweeper()
    yield


app = FastAPI(title="WinMarket AI", docs_url="/api/docs", redoc_url=None, lifespan=_lifespan)

app.mount("/static", StaticFiles(directory="static"), name="static")

# B13-T2: raw ASGI-level guard against unbounded upload accumulation before
# multipart parsing even starts — see src/web/body_limit_middleware.py.
#
# Registered here, BEFORE `inject_current_user_state`'s
# @app.middleware("http") below — deliberately, and NOT simply to be
# "outermost" (an earlier version of this comment argued that; it was
# wrong, see below). Starlette's add_middleware prepends to the stack, so
# whichever add_middleware call runs LAST ends up OUTERMOST; since
# @app.middleware("http") calls add_middleware(BaseHTTPMiddleware, ...) at
# decoration time, registering this call BEFORE that decorator makes THIS
# middleware the INNER one (closer to the routes), with
# inject_current_user_state's BaseHTTPMiddleware wrapping it.
#
# This matters for a subtle reason traced during review: Starlette's
# BaseHTTPMiddleware relays the request body through its own internal
# anyio.create_task_group()-based receive proxy
# (starlette/middleware/base.py, BaseHTTPMiddleware.__call__/
# receive_or_disconnect). Any exception raised WHILE being awaited FROM
# WITHIN that task-group context gets wrapped into an ExceptionGroup by
# anyio on the way out — which is no longer `isinstance(exc, HTTPException)`,
# so FastAPI's own `except HTTPException: raise` (fastapi/routing.py) no
# longer matches it and it falls through to the generic
# `except Exception: raise HTTPException(400, "There was an error parsing
# the body")` instead, silently discarding the real 413/error_code. This
# was reproduced directly: with BodySizeLimitMiddleware OUTSIDE
# inject_current_user_state's BaseHTTPMiddleware, an oversized upload
# returned a generic 400 instead of 413. Registering this middleware INNER
# to it means Starlette's routing calls THIS middleware's guarded_receive
# directly (via BaseHTTPMiddleware's relay, but from OUTSIDE any task-group
# context — the relay's `async with create_task_group()` block has already
# exited normally by the time guarded_receive's own size check runs), so
# RequestBodyTooLargeError propagates as itself, unwrapped, and is handled
# correctly by html_http_exception_handler below via ordinary
# HTTPException/MRO-based lookup. Verified end-to-end with a real oversized
# multipart POST through the full app (not just a hand-built ASGI scope).
app.add_middleware(
    BodySizeLimitMiddleware,
    max_bytes=config.MAX_REQUEST_BODY_MB * 1024 * 1024,
    guarded_prefixes=("/api/analyze", "/api/scoring-config/simulate", "/api/knowledge/documents"),
    # Lot 47 bis: ONLY /api/analyze carries the AO dossier (up to DOSSIER_MAX_TOTAL_BYTES of files
    # plus a bounded multipart envelope); the simulation and the RAG corpus keep MAX_REQUEST_BODY_MB.
    prefix_limits={"/api/analyze": config.DOSSIER_MAX_TOTAL_BYTES + config.DOSSIER_MULTIPART_MARGIN_BYTES},
)


@app.exception_handler(HTTPException)
async def html_http_exception_handler(request: Request, exc: HTTPException):
    """/api/* keeps plain JSON (existing clients expect it); every other
    route renders a normal page instead of raw JSON like
    {"detail":"Method Not Allowed"} — this is what a user hit when an
    already-open page's stale CSRF token 403'd and a refresh replayed the
    POST as a GET against a POST-only route.

    B13-T2 (coordinator-integration fix, found by review): also handles
    `src.web.body_limit_middleware.RequestBodyTooLargeError` — deliberately
    an `HTTPException` subclass rather than a plain `Exception` precisely so
    it reaches this SAME generic handler via ordinary MRO-based lookup, no
    bespoke handler required. See that class's docstring for why a plain
    `Exception` raised from inside `request.form()`'s multipart read gets
    silently reclassified into a generic 400 by fastapi/routing.py before
    ever reaching a handler registered for its own type.
    """
    # B14-T1 (coordinator-integration fix, found while adding real 429
    # responses with a Retry-After header — rate_limit.py/routes_auth.py,
    # routes_api.py): both branches below used to construct their Response
    # WITHOUT `headers=exc.headers`, so any header set on the raised
    # HTTPException (Retry-After being the concrete case that surfaced
    # this) was silently dropped before ever reaching the client — the
    # status code and detail came through fine, masking the loss. Confirmed
    # via a real TestClient request (not just reading the code): a 429
    # raised with `headers={"Retry-After": "..."}` arrived at the client
    # with no such header at all until this fix. `HTTPException.headers` is
    # `None` unless a route explicitly sets it, so `headers=exc.headers` is
    # a no-op for every pre-existing call site that never set headers.
    if request.url.path.startswith("/api/"):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)
    return templates.TemplateResponse(
        request, "error.html",
        {"status_code": exc.status_code, "detail": exc.detail},
        status_code=exc.status_code,
        headers=exc.headers,
    )


@app.middleware("http")
async def inject_current_user_state(request: Request, call_next):
    """Makes request.state.current_user / is_active_starter available to
    every template (Jinja2Templates always exposes `request`) so public
    pages (landing, header) can show the right CTA without each route
    wiring the lookup manually. This is informational only — it never
    blocks a request; /app/* and /api/* routes enforce access themselves
    (src/web/auth/dependencies.py) regardless of what this sets.
    """
    request.state.current_user = None
    request.state.is_active_starter = False

    from src.web.database.session import is_database_configured
    if is_database_configured():
        try:
            from src.web.auth.dependencies import get_current_user, user_has_active_starter_subscription
            from src.web.database.session import session_scope

            with session_scope() as db:
                user = get_current_user(request, db)
                request.state.current_user = user
                request.state.is_active_starter = user_has_active_starter_subscription(db, user)
        except Exception:
            pass  # never let this best-effort lookup break page rendering

    return await call_next(request)


app.include_router(pages_router)
app.include_router(auth_router)
app.include_router(account_router)
app.include_router(api_router)
app.include_router(knowledge_documents_router)
app.include_router(scoring_policy_router)
