from types import SimpleNamespace

from dfh.sql_dialect import days_between, date_plus_days, date_plus_literal_days


def engine(name):
    return SimpleNamespace(dialect=SimpleNamespace(name=name))


def test_sqlite_date_expressions():
    db = engine("sqlite")
    assert days_between(db, "later", "earlier") == (
        "CAST(julianday(later) - julianday(earlier) AS INTEGER)"
    )
    assert date_plus_days(db, ":today", "window") == (
        "date(:today, '+' || :window || ' day')"
    )
    assert date_plus_literal_days(db, ":today", 30) == "date(:today, '+30 day')"


def test_postgresql_date_expressions():
    db = engine("postgresql")
    assert days_between(db, "later", "earlier") == (
        "(CAST(later AS DATE) - CAST(earlier AS DATE))"
    )
    assert date_plus_days(db, ":today", "window") == (
        "(CAST(:today AS DATE) + CAST(:window AS INTEGER))"
    )
    assert date_plus_literal_days(db, ":today", 30) == (
        "(CAST(:today AS DATE) + 30)"
    )


def test_literal_day_offset_is_normalized_to_integer():
    assert date_plus_literal_days(engine("postgresql"), "created_at", "2") == (
        "(CAST(created_at AS DATE) + 2)"
    )
