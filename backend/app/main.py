"""CallSight AI — FastAPI application entry point."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.app.config import get_settings
from backend.app.db import engine

settings = get_settings()


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
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)

# ── CORS ──────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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
from backend.app.api.profiles import router as profiles_router  # noqa: E402
from backend.app.api.outcomes import router as outcomes_router  # noqa: E402
from backend.app.api.corrections import router as corrections_router  # noqa: E402
from backend.app.api.quality import router as quality_router  # noqa: E402
from backend.app.api.oauth import router as oauth_router  # noqa: E402
from backend.app.api.conversations import router as conversations_router  # noqa: E402
from backend.app.api.webhooks import router as webhooks_router  # noqa: E402
from backend.app.api.email_push import router as email_push_router  # noqa: E402
from backend.app.api.feedback import router as feedback_router  # noqa: E402
from backend.app.api.evaluation import router as evaluation_router  # noqa: E402
from backend.app.api.experiments import router as experiments_router  # noqa: E402
from backend.app.api.campaigns import router as campaigns_router  # noqa: E402

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
app.include_router(profiles_router, prefix=settings.API_V1_PREFIX, tags=["profiles"])
app.include_router(outcomes_router, prefix=settings.API_V1_PREFIX, tags=["outcomes"])
app.include_router(corrections_router, prefix=settings.API_V1_PREFIX, tags=["corrections"])
app.include_router(quality_router, prefix=settings.API_V1_PREFIX, tags=["quality"])
app.include_router(oauth_router, prefix=settings.API_V1_PREFIX, tags=["oauth"])
app.include_router(conversations_router, prefix=settings.API_V1_PREFIX, tags=["conversations"])
app.include_router(webhooks_router, prefix=settings.API_V1_PREFIX, tags=["webhooks"])
app.include_router(email_push_router, prefix=settings.API_V1_PREFIX, tags=["email-push"])
app.include_router(feedback_router, prefix=settings.API_V1_PREFIX, tags=["feedback"])
app.include_router(evaluation_router, prefix=settings.API_V1_PREFIX, tags=["evaluation"])
app.include_router(experiments_router, prefix=settings.API_V1_PREFIX, tags=["experiments"])
app.include_router(campaigns_router, prefix=settings.API_V1_PREFIX, tags=["campaigns"])

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
