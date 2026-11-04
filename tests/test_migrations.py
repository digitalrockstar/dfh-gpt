from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


def test_baseline_migration_creates_and_tracks_schema(tmp_path, monkeypatch):
    database_path = tmp_path / "migration-test.db"
    database_url = f"sqlite:///{database_path}"
    monkeypatch.setenv("DATABASE_URL", database_url)

    config = Config("alembic.ini")
    command.upgrade(config, "head")
    command.upgrade(config, "head")  # Applying the current head is idempotent.

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert {"alembic_version", "app_user", "sale", "stock_ledger"} <= tables
        with engine.connect() as connection:
            version = connection.execute(text("select version_num from alembic_version")).scalar_one()
        assert version == "0001_baseline"
    finally:
        engine.dispose()
