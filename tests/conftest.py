"""Shared pytest fixtures and path setup for the test suite."""

import os
import sys

# Add project root so ``backend.*`` imports work when pytest is invoked
# from anywhere (e.g. ``pytest tests/``).
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# Stub the envvars that ``backend.app.config`` requires at import time so
# tests don't need a real .env file.  These values are never used by the
# pure-logic tests; endpoint tests mock the DB / Claude clients directly.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://user:pass@localhost/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-stub")
os.environ.setdefault("JWT_SECRET", "test-secret")
