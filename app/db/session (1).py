"""
app/db/session.py

CLEANED — see audit trail below.

Itica uses Supabase as the primary data store. This file is largely
vestigial scaffolding from before the Supabase pivot — kept minimal rather
than deleted outright in case a future feature genuinely needs a direct
Postgres connection.

REMOVED: get_db(), get_db_session() — confirmed zero remaining callers
  after app/routers/human_review.py was fixed (it took an unused
  `db: AsyncSession = Depends(get_db)` on every route — dead weight that
  could 500 every request if DATABASE_URL was ever unset) and
  app/services/audit_ledger.py was deleted (its only other caller, and
  itself non-functional — see DELETIONS.md).

  Before deploying this file, run:
    grep -rn "get_db\\b\\|get_db_session\\b" --include="*.py" .
  to confirm nothing else in the repo still imports them. If something
  does, restore those two functions rather than breaking that caller.

KEPT: init_db()/dispose_db() — called from main.py's lifespan, already a
  safe no-op when DATABASE_URL is unset, and engine disposal needs to stay
  symmetric with get_engine() if the engine was ever created. init_db() no
  longer imports Base from app.models.models, since that class was removed
  in the models.py cleanup (it only ever backed AuditEvent, ExtractionResult,
  etc. — see models.py's own docstring). If you reintroduce real SQLAlchemy
  models later, re-add a Base import here and point Base.metadata.create_all
  at it.
"""
from __future__ import annotations

import logging
import os

from sqlalchemy.ext.asyncio import create_async_engine

logger = logging.getLogger(__name__)

engine = None


def get_engine():
    global engine
    if engine is None:
        DATABASE_URL = os.environ.get("DATABASE_URL")
        if not DATABASE_URL:
            logger.warning("DATABASE_URL not set — SQLAlchemy engine not initialised. Supabase path only.")
            return None
        engine = create_async_engine(
            DATABASE_URL,
            echo=False,
            pool_size=20,
            max_overflow=10,
            pool_pre_ping=True,
            pool_recycle=3600,
        )
    return engine


async def init_db():
    """
    No-op unless DATABASE_URL is set AND there are live ORM models to create
    tables for. There currently are none (see models.py) — this function is
    kept only so main.py's lifespan call doesn't need editing, and so it's
    a one-line change to wire up real models later if needed.
    """
    eng = get_engine()
    if eng is None:
        logger.info("init_db skipped — no DATABASE_URL configured")
        return
    logger.info("init_db: DATABASE_URL is set, but no SQLAlchemy models are currently registered to create")


async def dispose_db():
    global engine
    if engine:
        await engine.dispose()
        engine = None
        logger.info("SQLAlchemy engine disposed")
