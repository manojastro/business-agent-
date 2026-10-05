"""Application database engine and session helpers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


@lru_cache
def app_engine() -> Engine:
    s = get_settings()
    return create_engine(
        s.app_database_url,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=10,
        connect_args={"application_name": "metric-investigator-app"},
    )


@lru_cache
def _factory() -> sessionmaker[Session]:
    return sessionmaker(bind=app_engine(), expire_on_commit=False)


def new_session() -> Session:
    return _factory()()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Commit on success, roll back on error."""
    s = new_session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    s = new_session()
    try:
        yield s
    finally:
        s.close()


def psycopg_conninfo(sqlalchemy_url: str) -> str:
    """Convert a SQLAlchemy URL to a libpq URL for direct psycopg use (LangGraph checkpointer)."""
    return sqlalchemy_url.replace("postgresql+psycopg://", "postgresql://", 1)
