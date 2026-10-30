"""Database engine, async session factory, connection pooling, and lifecycle helpers."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import AsyncGenerator, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.db.models import Base

logger = logging.getLogger(__name__)

DEFAULT_SQLITE_PATH = Path.cwd() / "data" / "doc_intelligence.db"


def get_database_url() -> str:
    """Retrieve and normalize database URL for async drivers."""
    url = os.getenv("DATABASE_URL")
    if not url:
        DEFAULT_SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite+aiosqlite:///{DEFAULT_SQLITE_PATH.as_posix()}"

    # Normalize standard postgres URLs to asyncpg
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgresql://") and "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)

    return url


_async_engine: Optional[AsyncEngine] = None
_session_factory: Optional[async_sessionmaker[AsyncSession]] = None


def get_engine(database_url: Optional[str] = None) -> AsyncEngine:
    """Retrieve or create singleton AsyncEngine."""
    global _async_engine, _session_factory
    url = database_url or get_database_url()

    if _async_engine is None or str(_async_engine.url) != url:
        connect_args = {}
        if "sqlite" in url:
            connect_args["check_same_thread"] = False

        _async_engine = create_async_engine(
            url,
            echo=False,
            future=True,
            connect_args=connect_args,
        )
        _session_factory = async_sessionmaker(
            bind=_async_engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
        logger.info(f"Initialized database engine with URL dialect: {_async_engine.dialect.name}")

    return _async_engine


def get_session_factory(engine: Optional[AsyncEngine] = None) -> async_sessionmaker[AsyncSession]:
    """Retrieve sessionmaker factory."""
    global _session_factory
    if _session_factory is None or engine is not None:
        eng = engine or get_engine()
        _session_factory = async_sessionmaker(
            bind=eng,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    return _session_factory


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding an async database session."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db(engine: Optional[AsyncEngine] = None) -> None:
    """Initialize database tables and pgvector extension if PostgreSQL."""
    eng = engine or get_engine()
    async with eng.begin() as conn:
        if eng.dialect.name == "postgresql":
            try:
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector;"))
                logger.info("Enabled pgvector extension on PostgreSQL.")
            except Exception as exc:
                logger.warning(f"Could not initialize pgvector extension: {exc}")

        await conn.run_sync(Base.metadata.create_all)
        logger.info(f"Database schema initialized successfully on {eng.dialect.name}.")


async def drop_db(engine: Optional[AsyncEngine] = None) -> None:
    """Drop all tables in database (used for testing cleanups)."""
    eng = engine or get_engine()
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        logger.info("Database schema dropped.")


if __name__ == "__main__":
    import asyncio
    print("[*] Initializing database...")
    asyncio.run(init_db())
    print("[+] Database initialized successfully.")
