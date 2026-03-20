"""
app/db/session.py

SQLAlchemy async session management.
NOTE: Itica uses Supabase as the primary data store. init_db() is a safe no-op
when DATABASE_URL is not set — it will not attempt to create Supabase tables.
The session/engine is only used by routers that explicitly call get_db().
"""
from __future__ import annotations

import logging
import os

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

logger = logging.getLogger(__name__)

engine = None
async_session_factory = None


def get_engine():
    global engine, async_session_factory
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
        async_session_factory = sessionmaker(
            engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    return engine


async def get_db():
    eng = get_engine()
    if eng is None or async_session_factory is None:
        raise RuntimeError("DATABASE_URL is not configured — SQLAlchemy session unavailable")
    async with async_session_factory() as session:
        try:
            yield session
        finally:
            await session.close()


async def get_db_session():
    get_engine()
    if async_session_factory is None:
        raise RuntimeError("DATABASE_URL is not configured")
    return async_session_factory()


async def init_db():
    """
    Attempt to create SQLAlchemy-managed tables.
    Safe no-op if DATABASE_URL is not set (Supabase-only deployments).
    """
    eng = get_engine()
    if eng is None:
        logger.info("init_db skipped — no DATABASE_URL configured")
        return
    try:
        from app.models.models import Base
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("SQLAlchemy tables created/verified")
    except Exception as e:
        logger.warning(f"init_db error (non-fatal for Supabase deployments): {e}")


async def dispose_db():
    global engine
    if engine:
        await engine.dispose()
        engine = None
        logger.info("SQLAlchemy engine disposed")
