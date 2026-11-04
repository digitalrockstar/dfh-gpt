import pytest
from sqlalchemy import insert

from dfh.db import get_engine
from dfh.schema import metadata
from dfh.seed.run import assert_seed_database_empty


def test_empty_database_is_safe_to_seed():
    engine = get_engine("sqlite://")
    metadata.create_all(engine)
    try:
        assert_seed_database_empty(engine)
    finally:
        engine.dispose()


def test_seed_refuses_database_with_existing_rows():
    engine = get_engine("sqlite://")
    metadata.create_all(engine)
    try:
        role = metadata.tables["role"]
        with engine.begin() as connection:
            connection.execute(insert(role).values(code="admin", name="Administrator"))
        with pytest.raises(RuntimeError, match="non-empty database.*role"):
            assert_seed_database_empty(engine)
    finally:
        engine.dispose()
