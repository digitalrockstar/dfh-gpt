"""Small SQL fragments for expressions whose syntax differs by database dialect."""


def days_between(engine, later, earlier):
    """Return whole calendar days from earlier to later."""
    if engine.dialect.name == "sqlite":
        return f"CAST(julianday({later}) - julianday({earlier}) AS INTEGER)"
    return f"(CAST({later} AS DATE) - CAST({earlier} AS DATE))"


def date_plus_days(engine, date_expr, days_param):
    """Return a date expression advanced by a bound integer day count."""
    if engine.dialect.name == "sqlite":
        return f"date({date_expr}, '+' || :{days_param} || ' day')"
    return f"(CAST({date_expr} AS DATE) + (:{days_param} * INTERVAL '1 day'))"


def date_plus_literal_days(engine, date_expr, days):
    """Return a date expression advanced by a trusted integer literal."""
    days = int(days)
    if engine.dialect.name == "sqlite":
        return f"date({date_expr}, '+{days} day')"
    return f"(CAST({date_expr} AS DATE) + {days})"
