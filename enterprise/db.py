"""Database engine/session handling for the enterprise tables."""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import JSON, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

# JSONB on Postgres, JSON (text) elsewhere.
JSONVariant = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


def normalize_url(url: str) -> str:
    """Use the psycopg2 driver pinned in requirements.txt for bare postgres URLs.

    SQLAlchemy 2.1 maps ``postgresql://`` to psycopg 3, which is not a dependency.
    """
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg2://" + url[len(prefix):]
    return url


def make_engine(url: str) -> Engine:
    url = normalize_url(url)
    kwargs: dict = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    return engine


class Database:
    def __init__(self, url: str):
        self.url = url
        self.engine = make_engine(url)
        self.SessionLocal = sessionmaker(self.engine, expire_on_commit=False)

    def session(self) -> Session:
        return self.SessionLocal()

    @contextmanager
    def session_scope(self) -> Iterator[Session]:
        session = self.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    def create_all(self) -> None:
        """Create tables directly. For tests and local SQLite only; use Alembic elsewhere."""
        from enterprise import models  # noqa: F401  (register mappers)

        Base.metadata.create_all(self.engine)
