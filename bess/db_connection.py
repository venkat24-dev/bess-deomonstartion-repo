"""Lazy SQLAlchemy connectivity for the PostgreSQL application schema."""
from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from threading import Lock

from dotenv import load_dotenv
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session, sessionmaker


class DatabaseConfigurationError(RuntimeError):
    """Raised when the database connection is not configured correctly."""


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None
_engine_lock = Lock()
_dotenv_loaded = False


def _load_local_environment() -> None:
    global _dotenv_loaded
    if not _dotenv_loaded:
        load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
        _dotenv_loaded = True


def database_url() -> URL:
    """Return a parsed URL without logging or manually handling credentials."""
    _load_local_environment()
    from os import environ

    value = environ.get("DATABASE_URL")
    if not value:
        raise DatabaseConfigurationError(
            "DATABASE_URL is not configured. Set it in the environment or local .env file."
        )
    try:
        url = make_url(value)
    except (TypeError, ValueError) as exc:
        raise DatabaseConfigurationError("DATABASE_URL is not a valid SQLAlchemy URL.") from exc

    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+psycopg")
    if url.drivername != "postgresql+psycopg":
        raise DatabaseConfigurationError(
            "DATABASE_URL must use PostgreSQL with the psycopg 3 driver."
        )
    return url


def get_engine() -> Engine:
    """Create the pooled engine on first use; engine creation does not connect."""
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = create_engine(
                    database_url(),
                    pool_size=5,
                    max_overflow=5,
                    pool_timeout=30,
                    pool_pre_ping=True,
                    pool_recycle=1800,
                    connect_args={"connect_timeout": 5},
                )
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    """Return the shared factory; callers own each returned session."""
    global _session_factory
    if _session_factory is None:
        engine = get_engine()
        with _engine_lock:
            if _session_factory is None:
                _session_factory = sessionmaker(
                    bind=engine,
                    autoflush=False,
                    expire_on_commit=False,
                )
    return _session_factory


def get_db_session() -> Generator[Session, None, None]:
    """FastAPI dependency with rollback on failure and guaranteed close."""
    session = get_session_factory()()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def check_database_health(engine: Engine | None = None) -> bool:
    """Run a lightweight connectivity check, propagating database failures."""
    active_engine = engine or get_engine()
    with active_engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    return True


def dispose_database_engine() -> None:
    """Dispose pooled connections, primarily for application shutdown/tests."""
    global _engine, _session_factory
    with _engine_lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None
        _session_factory = None


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create an isolated factory for tests or explicitly managed engines."""
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
