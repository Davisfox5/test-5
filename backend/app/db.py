"""Database engine, session factory, and base model for SQLAlchemy async."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from backend.app.config import get_settings

settings = get_settings()

# Runtime engines connect as the non-owner ``linda_app`` role when
# APP_DATABASE_URL is set — table owners bypass RLS, so the owner DSN
# (DATABASE_URL) is reserved for Alembic/admin. See backend/app/rls.py.
# Convert postgres:// to postgresql+asyncpg:// and handle sslmode for asyncpg
_db_url = settings.APP_DATABASE_URL or settings.DATABASE_URL
if _db_url.startswith("postgres://"):
    _db_url = _db_url.replace("postgres://", "postgresql+asyncpg://", 1)
elif _db_url.startswith("postgresql://"):
    _db_url = _db_url.replace("postgresql://", "postgresql+asyncpg://", 1)

# asyncpg uses 'ssl' not 'sslmode' — strip sslmode from URL and pass ssl=True via connect_args
import ssl as _ssl_module
_connect_args = {}
if "sslmode=" in _db_url:
    _db_url = _db_url.split("?")[0]  # strip query params
    _ssl_ctx = _ssl_module.create_default_context()
    _ssl_ctx.check_hostname = False
    _ssl_ctx.verify_mode = _ssl_module.CERT_NONE
    _connect_args = {"ssl": _ssl_ctx}

# Pool sized for FastAPI multi-process Fly deployment. The api process
# group runs N machines x 2 uvicorn workers, and Celery worker / beat
# share the same Neon database (Celery uses its own sync engine on a
# separate 5+5 pool — see tasks.py). With Neon's default 100-connection
# cap, the prior 50+20 here meant a single api machine could exhaust the
# whole quota under load. 15+5 leaves headroom for ~3 machines worth of
# api + the worker pool + admin/maintenance access without hitting Neon's
# pgbouncer cap.
engine = create_async_engine(
    _db_url,
    echo=settings.DEBUG,
    pool_size=15,
    max_overflow=5,
    pool_pre_ping=True,
    connect_args=_connect_args,
)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# Registers the global after_begin listener that re-arms the tenant GUC
# (app.current_tenant) on every Postgres transaction — the plumbing the
# RLS policies key on. Import for side effect; must come after engine
# creation so no session can exist before the listener does.
import backend.app.tenant_ctx  # noqa: E402,F401


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    """FastAPI dependency — yields a session and ensures cleanup."""
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
