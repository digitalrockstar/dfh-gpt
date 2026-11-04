import os

import pytest
from sqlalchemy import inspect

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
    finally:
        metadata.drop_all(engine)
        engine.dispose()
