"""CallSight AI — FastAPI application entry point."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from backend.app.config import get_settings
from backend.app.db import engine

logger = logging.getLogger(__name__)
settings = get_settings()


# ── Fail-fast startup checks ──────────────────────────────────
# Catch missing / insecure configuration at import time rather than on first
# request. Anything with a security impact that must never be silently
# permissive in production gets validated here.
def _validate_production_settings() -> None:
    if settings.DEBUG:
        # Dev tree — permissive defaults are acceptable. Loud logs only.
        if not settings.ALLOWED_ORIGINS:
            logger.warning(
                "ALLOWED_ORIGINS is empty; DEBUG=True so CORS middleware "
                "will accept localhost origins for development."
            )
        if not settings.TOKEN_ENCRYPTION_KEY:
            logger.warning(
                "TOKEN_ENCRYPTION_KEY is unset; token_crypto will mint an "
                "ephemeral per-process key because DEBUG=True."
            )
        return

    problems: list[str] = []
    if not settings.ALLOWED_ORIGINS:
        problems.append(
            "ALLOWED_ORIGINS is empty. Set it to a comma-separated list of "
            "origins (e.g., 'https://app.callsight.ai') or enable DEBUG."
        )
    if not settings.TOKEN_ENCRYPTION_KEY:
        problems.append(
            "TOKEN_ENCRYPTION_KEY is empty. Generate one with "
            "`python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\"`."
        )
    if not settings.ANTHROPIC_API_KEY:
        problems.append("ANTHROPIC_API_KEY is empty.")
    if problems:
        raise RuntimeError(
            "Refusing to start in production with insecure defaults:\n - "
            + "\n - ".join(problems)
        )


_validate_production_settings()


# ── Security headers middleware ───────────────────────────────
# Applies to every response, including the static marketing/demo site mount.
# The CSP + frame-ancestors pairing blocks clickjacking; X-Content-Type-Options
# nosniff prevents MIME confusion; HSTS is emitted only when we detect HTTPS
# (the reverse proxy terminates TLS in prod, so we respect the forwarded proto).
_CSP = (
    "default-src 'self'; "
    "style-src 'self' https://fonts.googleapis.com 'unsafe-inline'; "
    "font-src 'self' https://fonts.gstatic.com; "
    "script-src 'self'; "
    "img-src 'self' data:; "
    "connect-src 'self' ws: wss:; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        headers = response.headers
        headers.setdefault("Content-Security-Policy", _CSP)
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault(
            "Referrer-Policy", "strict-origin-when-cross-origin"
        )
        headers.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=()",
        )
        # Only advertise HSTS on HTTPS requests. Emitting it over plain HTTP
        # is at best ignored and at worst flagged by scanners.
        forwarded_proto = request.headers.get("x-forwarded-proto", "").lower()
        if request.url.scheme == "https" or forwarded_proto == "https":
            headers.setdefault(
                "Strict-Transport-Security",
                "max-age=63072000; includeSubDomains; preload",
            )
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown hooks."""
    # ── Startup ──
    # DB tables are managed by Alembic, but we verify connectivity
    async with engine.begin() as conn:
        await conn.execute(__import__("sqlalchemy").text("SELECT 1"))

    yield

    # ── Shutdown ──
    await engine.dispose()


app = FastAPI(
    title=settings.APP_NAME,
    version="0.1.0",
    docs_url="/api/docs" if settings.DEBUG else None,
    redoc_url="/api/redoc" if settings.DEBUG else None,
    openapi_url="/api/openapi.json" if settings.DEBUG else None,
    lifespan=lifespan,
)

# Security headers apply to every response; add before CORS so preflights
# get them too.
app.add_middleware(SecurityHeadersMiddleware)

# ── CORS ──────────────────────────────────────────────────
# Explicit methods + headers instead of "*" so a future accidental credential
# leak can't be swept up via a permissive preflight.
_dev_origins = (
    ["http://localhost:3000", "http://localhost:8000", "http://127.0.0.1:8000"]
    if settings.DEBUG and not settings.ALLOWED_ORIGINS
    else []
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS or _dev_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "X-CallSight-Event",
        "X-CallSight-Signature",
        "X-CallSight-Timestamp",
        "X-Request-ID",
    ],
    max_age=600,
)

# ── API Routers ───────────────────────────────────────────
from backend.app.api.interactions import router as interactions_router  # noqa: E402
from backend.app.api.health import router as health_router  # noqa: E402
from backend.app.api.api_keys import router as api_keys_router  # noqa: E402
from backend.app.api.call_library import router as call_library_router  # noqa: E402
from backend.app.api.search import router as search_router  # noqa: E402
from backend.app.api.comments import router as comments_router  # noqa: E402
from backend.app.api.contacts import router as contacts_router  # noqa: E402
from backend.app.api.analytics import router as analytics_router  # noqa: E402
from backend.app.api.knowledge_base import router as kb_router  # noqa: E402
from backend.app.api.scorecards import router as scorecards_router  # noqa: E402
from backend.app.api.action_items import router as action_items_router  # noqa: E402
from backend.app.api.admin import router as admin_router  # noqa: E402
from backend.app.api.campaigns import router as campaigns_router  # noqa: E402
from backend.app.api.conversations import router as conversations_router  # noqa: E402
from backend.app.api.crm import router as crm_router  # noqa: E402
from backend.app.api.email_push import router as email_push_router  # noqa: E402
from backend.app.api.evaluation import router as evaluation_router  # noqa: E402
from backend.app.api.experiments import router as experiments_router  # noqa: E402
from backend.app.api.feedback import router as feedback_router  # noqa: E402
from backend.app.api.oauth import router as oauth_router  # noqa: E402
from backend.app.api.onboarding import router as onboarding_router  # noqa: E402
from backend.app.api.webhooks import router as webhooks_router  # noqa: E402

app.include_router(health_router, prefix=settings.API_V1_PREFIX, tags=["health"])
app.include_router(interactions_router, prefix=settings.API_V1_PREFIX, tags=["interactions"])
app.include_router(api_keys_router, prefix=settings.API_V1_PREFIX, tags=["api-keys"])
app.include_router(call_library_router, prefix=settings.API_V1_PREFIX, tags=["library"])
app.include_router(search_router, prefix=settings.API_V1_PREFIX, tags=["search"])
app.include_router(comments_router, prefix=settings.API_V1_PREFIX, tags=["comments"])
app.include_router(contacts_router, prefix=settings.API_V1_PREFIX, tags=["contacts"])
app.include_router(scorecards_router, prefix=settings.API_V1_PREFIX, tags=["scorecards"])
app.include_router(analytics_router, prefix=settings.API_V1_PREFIX, tags=["analytics"])
app.include_router(kb_router, prefix=settings.API_V1_PREFIX, tags=["knowledge-base"])
app.include_router(action_items_router, prefix=settings.API_V1_PREFIX, tags=["action-items"])
app.include_router(admin_router, prefix=settings.API_V1_PREFIX, tags=["admin"])
app.include_router(campaigns_router, prefix=settings.API_V1_PREFIX, tags=["campaigns"])
app.include_router(conversations_router, prefix=settings.API_V1_PREFIX, tags=["conversations"])
app.include_router(crm_router, prefix=settings.API_V1_PREFIX, tags=["crm"])
app.include_router(email_push_router, prefix=settings.API_V1_PREFIX, tags=["email-push"])
app.include_router(evaluation_router, prefix=settings.API_V1_PREFIX, tags=["evaluation"])
app.include_router(experiments_router, prefix=settings.API_V1_PREFIX, tags=["experiments"])
app.include_router(feedback_router, prefix=settings.API_V1_PREFIX, tags=["feedback"])
app.include_router(oauth_router, prefix=settings.API_V1_PREFIX, tags=["oauth"])
app.include_router(onboarding_router, prefix=settings.API_V1_PREFIX, tags=["onboarding"])
app.include_router(webhooks_router, prefix=settings.API_V1_PREFIX, tags=["webhooks"])

from backend.app.api.websocket import router as websocket_router  # noqa: E402

app.include_router(websocket_router, tags=["websocket"])


# ── Prometheus /metrics ──────────────────────────────────
from fastapi import Response  # noqa: E402

from backend.app.services import metrics as _ai_metrics  # noqa: E402


@app.get("/metrics", include_in_schema=False)
def prometheus_metrics() -> Response:
    payload, content_type = _ai_metrics.metrics_handler()
    return Response(content=payload, media_type=content_type)


# ── Static Files (minimal demo UI) ───────────────────────
app.mount("/", StaticFiles(directory="website", html=True), name="website")
