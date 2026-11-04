"""Database engine construction and URL normalization."""
import os

from sqlalchemy import create_engine, event


DB_URL = os.getenv("DATABASE_URL", "sqlite:///data/dfh.db")


def normalize_database_url(url: str) -> str:
    """Normalize hosted PostgreSQL URLs to the explicitly installed psycopg 3 driver."""
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def get_engine(url=None):
    database_url = normalize_database_url(url or DB_URL)
    eng = create_engine(database_url, future=True, pool_pre_ping=True)
    if eng.dialect.name == "sqlite":
        @event.listens_for(eng, "connect")
        def _fk(dbapi_con, _):
            dbapi_con.execute("PRAGMA foreign_keys=ON")
    return eng
