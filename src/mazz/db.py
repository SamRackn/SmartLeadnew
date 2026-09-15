"""Engine, session factory, and the two ways to get a Session.

- ``session_scope()`` — context manager for scripts, the pipeline worker, and
  tests. Commits on clean exit, rolls back on exception, always closes.
- ``get_session()`` — FastAPI dependency (``Depends(get_session)``). Same
  commit/rollback semantics, expressed as a generator.

The engine is created lazily so that importing ``mazz.db`` never touches the
database and tests can point ``DATABASE_URL`` somewhere else before first use.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from mazz.config import get_settings


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Return the process-wide SQLAlchemy engine."""

    return create_engine(
        get_settings().database_url,
        pool_pre_ping=True,
        future=True,
    )


@lru_cache(maxsize=1)
def get_sessionmaker() -> sessionmaker[Session]:
    """Return the process-wide session factory."""

    return sessionmaker(
        bind=get_engine(),
        autoflush=False,
        expire_on_commit=False,
        future=True,
    )


def dispose_engine() -> None:
    """Tear down the engine and factory (tests, or after a DB reset)."""

    if get_engine.cache_info().currsize:
        get_engine().dispose()
    get_sessionmaker.cache_clear()
    get_engine.cache_clear()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope around a series of operations.

        with session_scope() as session:
            session.add(lead)
    """

    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped Session.

        @app.get("/leads")
        def list_leads(session: Session = Depends(get_session)): ...
    """

    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
