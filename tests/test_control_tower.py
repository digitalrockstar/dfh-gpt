import os, pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from dfh.app import create_app
from tests.test_api import tok

DB = os.getenv("DFH_DB", "sqlite:///data/dfh.db")


@pytest.fixture(scope="module")
def client():
    return TestClient(create_app(DB, os.getenv("DFH_DATA", "data"), serve_frontend=False))


@pytest.fixture(autouse=True)
def restore_inbox():
    yield
    with create_engine(DB).begin() as c:
        c.execute(text("delete from exception_item")); c.execute(text("delete from kpi_snapshot"))


def test_headline(client):
    h = tok(client); r = client.get("/api/ct/headline?days=30", headers=h).json()
    assert r["store_stock_cost"] > 0 and r["warehouse_stock_cost"] > 0 and 5 < r["cover_days"] < 400
    assert 0 < r["wastage_pct_sales"] < 0.1 and r["gm_after_wastage"] < r["gm"]
    one = client.get("/api/ct/headline", headers=tok(client, "storemgr")).json()
    assert one["warehouse_stock_cost"] == 0 and one["store_stock_cost"] < r["store_stock_cost"]


def test_format_filter_and_league_size(client):
    h = tok(client); f = client.get("/api/ct/filters", headers=h).json(); assert "Flagship" in f["formats"]
    a = client.get("/api/ct/summary?fmt=Flagship", headers=h).json()["current"]["stores"]; assert a == 107
    assert len(client.get("/api/ct/league?limit=25", headers=h).json()["rows"]) == 25
    assert all(x["tier"] == "C" for x in client.get("/api/ct/league?fmt=Mall%20Kiosk", headers=h).json()["rows"])


def test_compare(client):
    h = tok(client)
    for m in ("wow", "mom"):
        r = client.get(f"/api/ct/compare?mode={m}", headers=h).json()
        assert r["net"] > 0 and r["prev"] > 0 and r["like_for_like"] and abs(sum(c["net"] for c in r["categories"]) - r["net"]) <= len(r["categories"])
    all_ = client.get("/api/ct/compare?mode=mom&lfl=0", headers=h).json(); lfl = client.get("/api/ct/compare?mode=mom&lfl=1", headers=h).json()
    assert lfl["stores"] < 500 and all_["stores"] is None
    assert client.get("/api/ct/compare?mode=yoy", headers=h).status_code == 422


def test_store_and_sku_drill(client):
    h = tok(client); lg = client.get("/api/ct/league", headers=h).json()["rows"][0]
    d = client.get(f"/api/ct/store/{lg['id']}", headers=h).json()
    assert d["store"]["id"] == lg["id"] and len(d["top_skus"]) == 12 and abs(sum(m["share"] for m in d["mix"]) - 1) < 1e-6 and d["current"]["net"] > 0
    k = client.get(f"/api/ct/store/{lg['id']}/sku/{d['top_skus'][0]['id']}", headers=h).json()
    assert k["daily"] and sum(x["net"] for x in k["daily"]) > 0
    sm = tok(client, "storemgr"); assert client.get("/api/ct/store/21", headers=sm).status_code == 200
    assert client.get("/api/ct/store/22", headers=sm).status_code == 403 and client.get("/api/ct/store/99999", headers=h).status_code == 404


def test_warehouses(client):
    r = client.get("/api/ct/warehouses", headers=tok(client)).json()["rows"]
    assert len(r) == 20 and all(x["stock_cost"] > 0 for x in r)
    assert abs(sum(x["ageing"][k] for x in r for k in x["ageing"]) - sum(x["stock_cost"] for x in r)) <= 100   # rounding only
    hyd = [x for x in r if x["loss_flag"]]; assert hyd and "Hyderabad" in hyd[0]["city"]
    wh = client.get("/api/ct/warehouses", headers=tok(client, "warehouse")).json()["rows"]; assert len(wh) == 1
    assert client.get("/api/ct/warehouses", headers=tok(client, "storemgr")).status_code == 403


def test_inbox_assign_and_rules(client):
    h = tok(client); r = client.get("/api/ct/inbox", headers=h).json()
    assert r["open"] >= 8 and r["unassigned"] == r["open"] and r["can_assign"]
    assert client.get("/api/ct/inbox", headers=h).json()["open"] == r["open"]           # sync is idempotent
    item = next(x for x in r["rows"] if x["severity"] == "high")
    fin = next(o for o in client.get("/api/ct/owners", headers=h).json() if o["role"] == "finance")
    p = lambda body, hh=h: client.patch(f"/api/ct/inbox/{item['id']}", json=body, headers=hh)
    assert p({"status": "in_progress"}).status_code == 422                                # no owner yet
    assert p({"owner_id": fin["id"]}).json()["status"] == "assigned"
    assert p({"due_date": "2020-01-01"}).status_code == 422 and p({"owner_id": 9999}).status_code == 422
    assert p({"owner_id": fin["id"]}, tok(client, "finance")).status_code == 403          # only CEO or admin assign
    assert p({"status": "in_progress"}, tok(client, "procurement")).status_code == 403    # not the owner
    assert p({"status": "in_progress"}, tok(client, "finance")).status_code == 200
    assert p({"status": "resolved"}).status_code == 200
    act = client.get("/api/ct/inbox", headers=h).json(); assert item["id"] not in [x["id"] for x in act["rows"]] and act["open"] == r["open"] - 1
    assert item["id"] in [x["id"] for x in client.get("/api/ct/inbox?status=resolved", headers=h).json()["rows"]]
    with create_engine(DB).connect() as c:
        assert c.execute(text("select count(*) from audit_log where action='exception_update' and row_id=:i"), dict(i=str(item["id"]))).scalar() >= 3


def test_inbox_scope(client):
    client.get("/api/ct/inbox", headers=tok(client))
    sm = client.get("/api/ct/inbox", headers=tok(client, "storemgr")).json()
    assert all(x["location_id"] == 21 for x in sm["rows"]) and not sm["can_assign"]


def test_snapshot(client):
    h = tok(client); s = client.get("/api/ct/snapshot", headers=h).json()
    assert "Net sales" in s["text"] and "—" not in s["text"] and not s["saved"]
    assert client.get("/api/ct/snapshot?save=1", headers=h).json()["saved"]
    assert client.get("/api/ct/snapshot?save=1", headers=tok(client, "finance")).json()["saved"] is False
    with create_engine(DB).connect() as c: assert c.execute(text("select count(*) from kpi_snapshot")).scalar() == 4

def test_pos_bill_analytics_are_separate_and_scoped(client):
    h = tok(client)
    with create_engine(DB).connect() as db:
        store = db.execute(text("select store_id from sale where status!='void' group by store_id order by count(*) desc limit 1")).scalar()
        expected = db.execute(text("select count(*),coalesce(sum(net_paise),0),coalesce(avg(net_paise),0) from sale where store_id=:s and status!='void' and business_date between '2026-10-02' and '2026-10-31'"), {"s": store}).first()
        sale = db.execute(text("select id,invoice_no from sale where store_id=:s and status!='void' order by created_at_device desc limit 1"), {"s": store}).first()
        foreign = db.execute(text("select id from sale where store_id!=21 and status!='void' limit 1")).scalar()
    d = client.get(f"/api/ct/store/{store}?days=30", headers=h)
    assert d.status_code == 200
    ps = d.json()["pos_summary"]
    assert ps["separate_from_parquet"] is True and ps["source"] == "POS transactions only"
    assert ps["bills"] == expected[0] and ps["sales_paise"] == expected[1]
    assert ps["avg_bill_paise"] == int(expected[2])
    assert client.get("/api/ct/bills?size=100", headers=h).json()["total"] > 0
    detail = client.get(f"/api/ct/bills/{sale.id}", headers=h)
    assert detail.status_code == 200
    body = detail.json()
    assert body["bill"]["invoice_no"] == sale.invoice_no and body["lines"] and body["payments"]
    assert body["source"].startswith("POS transaction detail")
    assert all(x["qty_label"].endswith(("kg", "units")) for x in body["lines"])
    assert client.get(f"/api/ct/bills/{foreign}", headers=tok(client, "storemgr")).status_code == 403


def test_pos_bill_list_excludes_voids(client):
    h = tok(client)
    with create_engine(DB).begin() as db:
        row = db.execute(text("select id,store_id,status from sale where status!='void' limit 1")).first()
        assert row is not None
        before = client.get(f"/api/ct/bills?store_id={row.store_id}&size=100", headers=h).json()["total"]
        db.execute(text("update sale set status='void' where id=:i"), {"i": row.id})
    try:
        after = client.get(f"/api/ct/bills?store_id={row.store_id}&size=100", headers=h).json()["total"]
        assert after == before - 1
        assert client.get(f"/api/ct/bills/{row.id}", headers=h).status_code == 200
    finally:
        with create_engine(DB).begin() as db:
            db.execute(text("update sale set status=:s where id=:i"), {"s": row.status, "i": row.id})

