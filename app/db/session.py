from __future__ import annotations
import logging, os
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

logger = logging.getLogger(__name__)

engine = None
async_session_factory = None

def get_engine():
    global engine, async_session_factory
    if engine is None:
        DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql+asyncpg://user:password@localhost/itica")
        engine = create_async_engine(DATABASE_URL, echo=False, pool_size=20, max_overflow=10, pool_pre_ping=True, pool_recycle=3600)
        async_session_factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)
    return engine

async def get_db():
    get_engine()
    async with async_session_factory() as session:
        try:
            yield session
        finally:
            await session.close()

async def get_db_session():
    get_engine()
    return async_session_factory()

async def init_db():
    get_engine()
    from app.models.models import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

async def dispose_db():
    global engine
    if engine:
        await engine.dispose()
        engine = None
