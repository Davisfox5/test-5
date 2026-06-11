"""Seed the three production AI surfaces' baseline prompts as v1 active variants.

Run once after the c0a17e1bf001 migration.  Idempotent — re-running is a
no-op if active variants already exist.

    python -m backend.seed_prompt_variants
"""

from __future__ import annotations

import logging

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.config import get_settings
from backend.app.services.ai_analysis import ANALYSIS_SYSTEM_PROMPT
from backend.app.services.email_classifier import SYSTEM_PROMPT as CLASSIFIER_PROMPT
from backend.app.services.email_reply import SYSTEM_PROMPT as REPLY_PROMPT
from backend.app.services.prompt_variant_service import seed_default_variants

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("seed_prompt_variants")


def _sync_url() -> str:
    url = get_settings().DATABASE_URL
    if url.startswith("postgresql+asyncpg://"):
        url = url.replace("postgresql+asyncpg://", "postgresql://", 1)
    elif url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


def main() -> None:
    engine = create_engine(_sync_url(), pool_pre_ping=True)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()
    try:
        created = seed_default_variants(
            session,
            {
                "analysis": ANALYSIS_SYSTEM_PROMPT,
                "email_classifier": CLASSIFIER_PROMPT,
                "email_reply": REPLY_PROMPT,
            },
        )
        if created:
            log.info("Seeded baseline variants: %s", created)
        else:
            log.info("All surfaces already had active variants — nothing to do.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
