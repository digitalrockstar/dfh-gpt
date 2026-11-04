"""Module 5 seed: wastage entries and markdown suggestions. Batches and stock come from the Module 4 seed, which must run first."""
import random
from datetime import datetime, date, timedelta
import duckdb
from sqlalchemy import text, insert
from ..schema import metadata
from ..sql_dialect import days_between, date_plus_literal_days

NOW = datetime(2026, 10, 31, 9, 0)
START = datetime(2026, 5, 1)
MIX_STORE = [("EXPIRY", 22), ("RANCID", 6), ("PEST", 4), ("DAMAGE", 14), ("SPILL", 12), ("WEIGHT", 20), ("SAMPLE", 17), ("THEFT", 1), ("CUSTRET", 4)]
MIX_WH = [("EXPIRY", 18), ("RANCID", 10), ("PEST", 12), ("DAMAGE", 24), ("SPILL", 12), ("WEIGHT", 8), ("THEFT", 1), ("QREJECT", 15)]
MIX_F1 = [("EXPIRY", 8), ("RANCID", 5), ("PEST", 6), ("DAMAGE", 22), ("SPILL", 14), ("WEIGHT", 24), ("THEFT", 21)]


def _pick(rng, mix):
    return rng.choices([m[0] for m in mix], [m[1] for m in mix])[0]


def run(engine, seed=42, outdir="data"):
    rng = random.Random(seed + 5)
    T = metadata.tables
    with engine.connect() as c:
        skus = [dict(r._mapping) for r in c.execute(text("select id, code, name, base_uom, procurement_type, std_cost_paise, mrp_paise, shelf_life_days, popularity, bulk_sku_id, status from sku"))]
        locs = [dict(r._mapping) for r in c.execute(text("select id, type, tier, serving_wh_id from location where type in ('store','warehouse')"))]
        vend = {r[0]: r[1] for r in c.execute(text("select sku_id, min(vendor_id) from vendor_sku_cost group by 1"))}
        users = {r[0]: r[1] for r in c.execute(text("select username, id from app_user"))}
        f1 = __import__("json").loads(c.execute(text("select params from demo_finding where code='F1'")).scalar())["warehouse_id"]
        f6 = __import__("json").loads(c.execute(text("select params from demo_finding where code='F6'")).scalar())
        reasons = {r[0] for r in c.execute(text("select code from wastage_reason"))}
    pine, kiosks = list(f6["sku_ids"]), list(f6["store_ids"])
    active = [s for s in skus if s["status"] == "active" and s["procurement_type"] != "kit"]
    loose = [s for s in active if s["base_uom"] == "g"]
    pop = [max(1, s["popularity"] or 1) for s in active]
    popl = [max(1, s["popularity"] or 1) for s in loose]
    byid = {s["id"]: s for s in skus}
    stores = [l for l in locs if l["type"] == "store"]; whs = [l for l in locs if l["type"] == "warehouse"]
    cogs = {r[0]: r[1] for r in duckdb.connect().execute(f"select store_id, sum(cogs_paise) from read_parquet('{outdir}/sales_daily/**/*.parquet', hive_partitioning=true) group by 1").fetchall()}

    # ---- batches and stock come from Module 4; markdown suggestions read the near-expiry store balances ----
    with engine.connect() as c:
        bysku = {}
        for r in c.execute(text("select id, sku_id from batch")): bysku.setdefault(r[1], []).append({"id": r[0]})
        days_expr = days_between(engine, "t.expiry_date", ":d")
        expiry_limit = date_plus_literal_days(engine, ":d", 30)
        near = c.execute(text("select b.location_id, b.batch_id, b.qty, " + days_expr + " from stock_balance b join batch t on t.id=b.batch_id join location l on l.id=b.location_id "
                              "where l.type='store' and b.qty>0 and t.expiry_date>=:d and t.expiry_date<=" + expiry_limit + " order by b.location_id, b.batch_id"), dict(d=str(NOW.date()))).fetchall()
    batches, balrows, marks = [], [], []
    for loc, b, q, left in near:
        marks.append(dict(location_id=loc, batch_id=b, suggested_pct=40.0 if left <= 7 else 30.0 if left <= 15 else 20.0, reason=f"{left} days to expiry, {q} in stock", created_at=NOW, status="open"))

    # ---- wastage entries ----
    ent = []
    uid_wh, uid_sm = users.get("warehouse"), users.get("storemgr")
    boss = [users.get("finance"), users.get("ceo")]

    def mk(loc, ts, reason, sku, qty, is_wh):
        ts = min(ts, NOW - timedelta(minutes=rng.randint(30, 600)))
        s = byid[sku]; cost = round(qty / 1000 * s["std_cost_paise"]) if s["base_uom"] == "g" else qty * s["std_cost_paise"]
        bl = bysku.get(sku); b = rng.choice(bl)["id"] if bl and rng.random() < 0.5 else None
        st = "approved"; ap = rng.choice(boss)
        if cost >= 200000 and ts >= NOW - timedelta(days=7): st, ap = "pending", None
        elif rng.random() < 0.012: st = "rejected"
        ent.append(dict(ts=ts, location_id=loc, sku_id=sku, batch_id=b, qty=qty, reason=reason, cost_paise=cost, entered_by=uid_wh if loc == 1 else uid_sm if loc == 21 else None, status=st, approved_by=ap,
                        photo_ref=f"photo_{rng.randint(1000, 9999)}.jpg" if reason in ("DAMAGE", "PEST", "RANCID", "QREJECT") and rng.random() < 0.6 else None, vendor_claim=reason in ("QREJECT", "PEST", "RANCID") and rng.random() < 0.5))

    def when(is_wh, night=False):
        d = START + timedelta(days=rng.random() * 183.9)
        h = rng.choice([22, 23, 0, 1, 2, 3, 4]) if night else (rng.randint(7, 19) if is_wh else rng.randint(9, 21))
        return d.replace(hour=h, minute=rng.randint(0, 59), second=0, microsecond=0)

    for l in stores:
        n = max(8, int(cogs.get(l["id"], 2e8) * 0.0075 / 45000 * rng.uniform(0.8, 1.25)))
        for _ in range(n):
            r = _pick(rng, MIX_STORE)
            s = rng.choices(loose, popl)[0] if r in ("WEIGHT", "SPILL", "SAMPLE") else rng.choices(active, pop)[0]
            q = rng.randint(30, 600) if s["base_uom"] == "g" else rng.randint(1, 3)
            mk(l["id"], when(False), r, s["id"], q, False)
    for l in whs:
        n = 380
        for i in range(n):
            if l["id"] == f1:
                ts = when(True); copies = {5: 1, 6: 1, 7: 1, 8: 2, 9: 3, 10: 4}[ts.month]
                r = _pick(rng, MIX_F1 if ts.month >= 8 and rng.random() < 0.7 else MIX_WH)
                if r == "THEFT": ts = when(True, night=True)
                for _k in range(copies):
                    s = rng.choices(active, pop)[0]; q = rng.randint(100, 1500) if s["base_uom"] == "g" else rng.randint(1, 8)
                    mk(l["id"], ts if _k == 0 else ts.replace(day=rng.randint(1, 28), hour=rng.randint(7, 19)), r, s["id"], q, True)
            else:
                r = _pick(rng, MIX_WH); s = rng.choices(active, pop)[0]
                q = rng.randint(100, 1500) if s["base_uom"] == "g" else rng.randint(1, 8)
                mk(l["id"], when(True), r, s["id"], q, True)
    pset = [byid[i] for i in pine if i in byid and byid[i]["base_uom"] != "g"]
    for st in kiosks:
        for _ in range(rng.randint(7, 12)):
            ts = START + timedelta(days=rng.randint(84, 183), hours=rng.randint(9, 21), minutes=rng.randint(0, 59))
            s = rng.choice(pset); mk(st, min(ts, NOW - timedelta(hours=2)), "EXPIRY", s["id"], rng.randint(3, 8), False)

    assert all(e["reason"] in reasons for e in ent)
    with engine.begin() as c:
        for name, rows in (("wastage_entry", ent), ("markdown_suggestion", marks)):
            for i in range(0, len(rows), 5000): c.execute(insert(T[name]), rows[i:i + 5000])
    return dict(batches=len(batches), stock_rows=len(balrows), entries=len(ent), markdowns=len(marks))
