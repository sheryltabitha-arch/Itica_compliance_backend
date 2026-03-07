"""
Itica — Database Session Management
SQLAlchemy async engine and session factory.
"""
from __future__ import annotations

import logging
import os

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://user:password@localhost/itica"
)

# Async engine
engine = create_async_engine(
    DATABASE_URL,
    echo=os.environ.get("SQL_ECHO", "false").lower() == "true",
    pool_size=20,
    max_overflow=10,
    pool_pre_ping=True,
    pool_recycle=3600,
)

# Session factory
async_session_factory = sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncSession:
    """
    Dependency: Yield an AsyncSession for each request.
    Automatically committed/rolled back by FastAPI.
    """
    async with async_session_factory() as session:
        try:
            yield session
        finally:
            await session.close()


async def get_db_session() -> AsyncSession:
    """
    Get a standalone session (not for use in request handlers).
    Caller is responsible for closing.
    """
    return async_session_factory()


async def init_db():
    """
    Initialize database: create tables if they don't exist.
    Called at application startup.
    """
    from app.models.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables initialized")


async def dispose_db():
    """
    Close all database connections.
    Called at application shutdown.
    """
    await engine.dispose()
    logger.info("Database engine disposed")