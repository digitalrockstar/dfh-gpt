from dfh.db import normalize_database_url


def test_render_postgres_url_uses_psycopg3():
    assert normalize_database_url("postgres://user:pass@host:5432/db") == (
        "postgresql+psycopg://user:pass@host:5432/db"
    )


def test_standard_postgresql_url_uses_psycopg3():
    assert normalize_database_url("postgresql://user:pass@host/db") == (
        "postgresql+psycopg://user:pass@host/db"
    )


def test_explicit_driver_and_sqlite_urls_are_preserved():
    assert normalize_database_url("postgresql+psycopg://user:pass@host/db") == (
        "postgresql+psycopg://user:pass@host/db"
    )
    assert normalize_database_url("sqlite:///data/dfh.db") == "sqlite:///data/dfh.db"
