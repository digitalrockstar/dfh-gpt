import os, json, pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from dfh.app import create_app
from dfh.db import get_engine

DB = os.getenv("DFH_DB", "sqlite:///data/dfh.db")


@pytest.fixture(scope="module")
def client():
    return TestClient(create_app(DB, os.getenv("DFH_DATA", "data"), serve_frontend=False))


@pytest.fixture(scope="module")
def eng():
    return get_engine(DB)


@pytest.fixture(scope="module")
def notes(eng):
    with eng.connect() as c:
        return json.loads(c.execute(text("select notes from forecast_run order by id desc limit 1")).scalar())


def tok(c, user="ceo"):
    r = c.post("/api/login", json=dict(username=user, password="demo1234")); assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def q(eng, sql, **p):
    with eng.connect() as c:
        return c.execute(text(sql), p).fetchall()


def test_forecast_shape(eng):
    n = q(eng, "select count(*) from forecast")[0][0]; a = q(eng, "select count(*) from store_assortment")[0][0]
    assert n == a * 8
    assert q(eng, "select count(*) from forecast where qty_forecast < 0 or lower < 0 or lower > qty_forecast + 0.01 or upper < qty_forecast - 0.01 or value_paise < 0")[0][0] == 0
    assert q(eng, "select count(distinct week_start) from forecast")[0][0] == 8
    assert q(eng, "select min(week_start) from forecast")[0][0] == "2026-11-01"


def test_festival_sensitivity_learned(notes):
    b = notes["beta"]
    assert all(v >= 0 for v in b.values())
    assert b["GFT"] > b["CHO"] > b["NDF"] > b["SED"]
    assert 2.4 < b["GFT"] < 3.6 and 0.8 < b["NDF"] < 1.6


def test_backtest_beats_flat_average(notes):
    lv = notes["backtest"]["levels"]
    assert lv["city_category_week"]["wape"] < 0.15
    for k in ("city_category_week", "warehouse_sku_week", "store_sku_week"):
        assert lv[k]["wape"] < lv[k]["wape_flat"], k
    assert 0.7 < notes["backtest"]["band"]["coverage"] < 0.92        # 80 percent band, checked on weeks not used to fit it
    assert notes["backtest"]["excluded"]["new_store_rows"] > 0


def test_festival_peak_in_chain_forecast(eng, notes):
    wk = [r[1] for r in q(eng, "select week_start, sum(value_paise) from forecast group by 1 order by 1")]; base = notes["chain_baseline_week_value"]
    assert wk[0] / base[0] > 1.5                                      # Diwali run-up
    assert abs(wk[-1] / (base[-1] * notes["realisation"]) - 1) < 0.05      # back to base once the calendar has no festival


def test_pack_demand_rolls_up_to_bulk(eng):
    # sku 11 is the 50 g pack of bulk sku 10. A warehouse need for sku 10 must include the packs in grams.
    r = q(eng, "select q.location_id, q.need_qty from demand_requirement q where q.sku_id=10 order by q.location_id")
    assert r
    for wh, need in r[:5]:
        packs = q(eng, "select coalesce(sum(f.qty_forecast), 0) from forecast f join location l on l.id=f.store_id where f.sku_id=11 and l.serving_wh_id=:w", w=wh)[0][0]
        assert need >= packs * 50 - 10


def test_suggestion_invariants(eng):
    rows = q(eng, "select suggested_qty, moq, unit_cost_paise, value_paise, urgency, cover_days, lead_time_days, order_by, on_hand, on_order from reorder_suggestion")
    assert len(rows) > 50
    for qty, moq, uc, val, urg, cov, lead, ob, oh, oo in rows:
        assert qty > 0 and qty >= moq and abs(val - qty * uc) <= 1
        assert urg in ("critical", "soon", "plan") and str(ob) >= "2026-10-31" and oh >= 0 and oo >= 0
        if urg == "critical": assert cov <= lead            # cover is stored rounded to 0.1 day
        if urg == "plan": assert cov - lead >= 7


def test_planted_findings(client, eng):
    # F3: Pune warehouse is short on the almonds and cashews that stock out in Pune stores
    r = q(eng, "select urgency from reorder_suggestion r join location l on l.id=r.location_id where l.city='Pune' and r.sku_id in (10, 64)")
    assert r and all(x[0] == "critical" for x in r)
    # F7: some gift boxes cannot be fully built from planned stock
    k = client.get("/api/demand/kits", headers=tok(client)).json()
    assert k["at_risk"] >= 1 and k["items"][0]["coverage"] < 0.9 and k["items"][0]["buildable"] < k["items"][0]["units"]


def test_endpoints_and_roles(client):
    ceo = tok(client)
    s = client.get("/api/demand/summary", headers=ceo).json()
    assert s["peak_lift"] > 0.5 and 0.85 < s["accuracy"] < 1 and s["critical"] > 0 and s["next4"] > 0
    assert client.get("/api/demand/forecast", headers=ceo).status_code == 200
    assert client.get("/api/demand/forecast?scope=category", headers=ceo).status_code == 422
    assert client.get("/api/demand/forecast?scope=nope&key=x", headers=ceo).status_code == 422
    f = client.get("/api/demand/forecast?scope=category&key=GFT", headers=ceo).json()
    assert len(f["actual"]) == 12 and len(f["forecast"]) == 8 and all(x["lower"] <= x["value"] <= x["upper"] for x in f["forecast"])
    assert client.get("/api/demand/summary", headers=tok(client, "cashier")).status_code == 403
    assert client.get("/api/demand/suggestions", headers=tok(client, "storemgr")).status_code == 403
    chain = sum(x["value"] for x in f["forecast"])
    sm = client.get("/api/demand/forecast", headers=tok(client, "storemgr")).json()
    assert 0 < sum(x["value"] for x in sm["forecast"]) < chain * 0.1
    ex = client.get("/api/ct/exceptions", headers=ceo).json()
    assert any(x["type"] == "replenish" for x in ex)


def test_warehouse_scope(client):
    h = tok(client, "warehouse"); me = client.get("/api/me", headers=h).json()
    r = client.get("/api/demand/suggestions?status=", headers=h).json()
    assert r["total"] > 0 and {x["location_id"] for x in r["items"]} == {me["scope_id"]} if me["scope_type"] == "warehouse" else True
    open_ = client.get("/api/demand/suggestions", headers=h).json()["items"]
    if open_: assert client.post(f"/api/demand/suggestions/{open_[0]['id']}/decision", headers=h, json=dict(action="approve")).status_code == 403


def test_decisions(client, eng):
    ceo, pro = tok(client), tok(client, "procurement")
    small = q(eng, "select id, moq, suggested_qty from reorder_suggestion where status='open' and value_paise < 200000000 and moq < suggested_qty order by id limit 3")
    big = q(eng, "select id from reorder_suggestion where status='open' and value_paise > 260000000 order by id limit 2")
    if len(small) < 3 or not big: pytest.skip("not enough open suggestions left; run python -m dfh.demand.run")
    i, moq, sug = small[0]
    d = f"/api/demand/suggestions/{i}/decision"
    assert client.post(d, headers=pro, json=dict(action="maybe")).status_code == 422
    assert client.post(d, headers=pro, json=dict(action="approve", qty=moq - 1)).status_code == 422
    assert client.post(d, headers=pro, json=dict(action="approve", qty=sug * 3 + 1)).status_code == 422
    assert client.post(d, headers=pro, json=dict(action="approve", qty=-5)).status_code == 422
    r = client.post(d, headers=pro, json=dict(action="approve", qty=sug)); assert r.status_code == 200 and r.json()["status"] == "approved"
    assert client.post(d, headers=pro, json=dict(action="reject")).status_code == 409
    assert q(eng, "select approved_qty, decided_by from reorder_suggestion where id=:i", i=i)[0][0] == sug
    assert q(eng, "select count(*) from audit_log where table_name='reorder_suggestion' and row_id=:i and action='approve'", i=str(i))[0][0] >= 1
    j = small[1][0]; r = client.post(f"/api/demand/suggestions/{j}/decision", headers=ceo, json=dict(action="reject", note="Using stock transfer instead")); assert r.status_code == 200 and r.json()["status"] == "rejected"
    b = big[0][0]
    assert client.post(f"/api/demand/suggestions/{b}/decision", headers=pro, json=dict(action="approve")).status_code == 403
    assert client.post(f"/api/demand/suggestions/{b}/decision", headers=ceo, json=dict(action="approve")).status_code == 200
    assert client.post("/api/demand/suggestions/99999999/decision", headers=ceo, json=dict(action="approve")).status_code == 404


def test_minmax_review(client, eng):
    h = tok(client, "storemgr"); me = client.get("/api/me", headers=h).json()
    r = client.get("/api/demand/minmax", headers=h).json()
    assert r["checked"] > 50 and r["total"] > 0 and {x["store_id"] for x in r["items"]} == {me["scope_id"]}
    x = r["items"][0]
    assert x["new_max"] > x["new_min"] >= 1 and abs(x["change"]) >= 0.25
    other = q(eng, "select id from location where type='store' and id != :s limit 1", s=me["scope_id"])[0][0]
    assert client.post("/api/demand/minmax", headers=h, json=dict(store_id=other, sku_id=x["sku_id"])).status_code == 403
    assert client.post("/api/demand/minmax", headers=tok(client, "cashier"), json=dict(store_id=me["scope_id"], sku_id=x["sku_id"])).status_code == 403
    ok = client.post("/api/demand/minmax", headers=h, json=dict(store_id=x["store_id"], sku_id=x["sku_id"])); assert ok.status_code == 200
    assert q(eng, "select min_qty, max_qty from store_assortment where store_id=:s and sku_id=:k", s=x["store_id"], k=x["sku_id"])[0] == (x["new_min"], x["new_max"])
    assert client.post("/api/demand/minmax", headers=h, json=dict(store_id=x["store_id"], sku_id=x["sku_id"])).status_code == 409
    assert q(eng, "select count(*) from audit_log where table_name='store_assortment' and action='minmax'")[0][0] >= 1


def test_live_demand_supply_uses_inventory_and_procurement_records(client, eng):
    h = tok(client)
    rows = client.get("/api/demand/suggestions?status=open&size=100", headers=h).json()["items"]
    assert rows
    row = rows[0]
    stock = q(eng, "select coalesce(sum(qty),0) from stock_balance where location_id=:l and sku_id=:s", l=row["location_id"], s=row["sku_id"])[0][0]
    orders = q(eng, "select coalesce(sum(pl.qty-coalesce(gr.received,0)),0) from purchase_order p join purchase_order_line pl on pl.po_id=p.id left join (select g.po_id,gl.sku_id,sum(gl.qty_received) received from grn g join grn_line gl on gl.grn_id=g.id where g.status='posted' group by g.po_id,gl.sku_id) gr on gr.po_id=p.id and gr.sku_id=pl.sku_id where p.dest_location_id=:l and pl.sku_id=:s and p.status in ('approved','confirmed','in_transit','partial')", l=row["location_id"], s=row["sku_id"])[0][0]
    assert row["on_hand"] == stock
    assert row["on_order"] == max(0, orders)
    assert row["available"] == row["on_hand"] + row["on_order"]
