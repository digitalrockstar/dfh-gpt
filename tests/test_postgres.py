import os

import pytest
from sqlalchemy import inspect, text

from dfh.db import get_engine
from dfh.schema import metadata


def test_schema_creates_on_postgresql():
    url = os.getenv("DFH_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("DFH_TEST_POSTGRES_URL is not configured")

    engine = get_engine(url)
    try:
        metadata.drop_all(engine)
        metadata.create_all(engine)
        table_names = set(inspect(engine).get_table_names())
        assert len(table_names) >= 90
        assert {"app_user", "sale", "stock_ledger", "journal_entry"} <= table_names

        # The application uses generated IDs when posting journal entries and
        # wastage records. PostgreSQL requires INSERT ... RETURNING, not
        # SQLite's cursor.lastrowid behavior.
        with engine.begin() as connection:
            role_id = connection.execute(
                text("insert into role(code, name) values ('pg_test', 'Postgres test') returning id")
            ).scalar_one()
            assert role_id > 0
    finally:
        metadata.drop_all(engine)
        engine.dispose()
