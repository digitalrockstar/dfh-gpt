"""python -m dfh.seed.run [--months 6] [--seed 42] [--db sqlite:///data/dfh.db] [--data data]"""
import argparse, os, random, time

from sqlalchemy import insert, select

from ..schema import metadata
from ..db import get_engine
from .masters import build_masters
from . import history, inventory, wastage, procurement, pos, finance, crm, people
from ..demand import model as demand


def assert_seed_database_empty(engine):
    """Refuse to layer demo fixtures over an existing database.

    SQLite demo runs intentionally recreate their local file before this check.
    For PostgreSQL and other server databases, this guard prevents duplicate seed
    rows and accidental contamination of a database containing real records.
    """
    with engine.connect() as connection:
        for table in metadata.sorted_tables:
            if connection.execute(select(table).limit(1)).first() is not None:
                raise RuntimeError(
                    f"Refusing to seed non-empty database: table '{table.name}' "
                    "already contains data. Use a dedicated empty demo database."
                )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=6); ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--db", default="sqlite:///data/dfh.db"); ap.add_argument("--data", default="data")
    ap.add_argument("--no-history", action="store_true")
    a = ap.parse_args()
    os.makedirs(a.data, exist_ok=True)
    if a.db.startswith("sqlite:///"):
        p = a.db.replace("sqlite:///", "")
        if os.path.exists(p):
            os.remove(p)
    eng = get_engine(a.db); t0 = time.time()
    metadata.create_all(eng)
    assert_seed_database_empty(eng)
    T, info = build_masters(random.Random(a.seed), a.seed)
    with eng.begin() as c:
        for t in metadata.sorted_tables:
            if t.name in T and T[t.name]:
                cols = {x.name for x in t.columns}
                keys = [k for k in cols if any(k in r for r in T[t.name])]
                rows = [{k: r.get(k, False if str(t.c[k].type) == "BOOLEAN" else None) for k in keys} for r in T[t.name]]
                for i in range(0, len(rows), 5000):
                    c.execute(insert(t), rows[i:i + 5000])
    print(f"masters loaded in {time.time() - t0:.1f}s")
    print("people:", people.run(eng, a.seed))
    if not a.no_history:
        t1 = time.time()
        print("history:", history.run(eng, a.months, a.seed, a.data), f"{time.time() - t1:.1f}s")
        t2 = time.time(); print("inventory:", inventory.run(eng, a.seed), f"{time.time() - t2:.1f}s")
        print("wastage:", wastage.run(eng, a.seed, a.data))
        print("demand:", demand.run(eng, a.data, a.seed))
        print("procurement:", procurement.run(eng, a.seed, a.data))
        print("pos:", pos.run(eng, a.seed, a.data))
        t3 = time.time(); print("finance:", finance.run(eng, a.seed, a.data), f"{time.time() - t3:.1f}s")
        t4 = time.time(); print("crm:", crm.run(eng, a.seed, a.data), f"{time.time() - t4:.1f}s")


if __name__ == "__main__":
    main()
