import os, uuid, pytest
from datetime import datetime, timedelta
from fastapi.testclient import TestClient
from sqlalchemy import text
from dfh.app import create_app
from dfh.db import get_engine
from dfh.pos_calc import line_calc, bill_calc

DB = os.getenv("DFH_DB", "sqlite:///data/dfh.db")


@pytest.fixture(scope="module")
def client():
    return TestClient(create_app(DB, os.getenv("DFH_DATA", "data"), serve_frontend=False))


@pytest.fixture(scope="module")
def eng():
    return get_engine(DB)


def tok(c, user):
    r = c.post("/api/login", json=dict(username=user, password="demo1234")); assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


@pytest.fixture(scope="module")
def boot(client):
    return client.get("/api/pos/bootstrap", headers=tok(client, "cashier")).json()


@pytest.fixture(scope="module")
def lab(client):
    """A different store from the demo store, driven by the CEO, so tests never use up the demo store's invoice numbers."""
    h = tok(client, "ceo"); st = client.get("/api/pos/bootstrap", headers=h).json()["stores"][-1]
    assert st["id"] != 21
    return dict(h=h, boot=client.get(f"/api/pos/bootstrap?store_id={st['id']}", headers=h).json(), store=st["id"])


def insert_sale(eng, boot, store_id, dev_id, items, when):
    """A bill written straight to the table for return tests, outside any device series."""
    sid = str(uuid.uuid4()); calc = [dict(zip(("gross", "net", "tax"), line_calc(s["uom"], s["price"], q, 0, s["tax_pct"])), discount=0) for s, q in items]; t = bill_calc(calc)
    with eng.begin() as c:
        c.execute(text("insert into sale(id, store_id, device_id, invoice_no, business_date, created_at_device, received_at, cashier_id, gross_paise, discount_paise, tax_paise, net_paise, round_off_paise, status, idem_key, currency) values (:i,:s,:d,:n,:b,:w,:w,null,:g,0,:t,:nt,:r,'paid',:k,'INR')"),
                  dict(i=sid, s=store_id, d=dev_id, n=f"TEST-{sid[:8]}", b=when.date(), w=when, g=t["gross"], t=t["tax"], nt=t["total"], r=t["round_off"], k=f"test-{sid}"))
        for (s, q), l in zip(items, calc):
            c.execute(text("insert into sale_line(sale_id, sku_id, qty, unit_price_paise, discount_paise, tax_rate, tax_paise, net_paise, hsn) values (:s,:k,:q,:p,0,:r,:t,:n,:h)"), dict(s=sid, k=s["id"], q=q, p=s["price"], r=s["tax_pct"], t=l["tax"], n=l["net"], h=s["hsn"]))
        c.execute(text("insert into payment(sale_id, mode, amount_paise, status) values (:s,'cash',:a,'ok')"), dict(s=sid, a=t["total"]))
    return f"TEST-{sid[:8]}"


def pick(boot, uom, n=1):
    return sorted((s for s in boot["catalog"] if s["uom"] == uom and s["price"] > 5000 and s["procurement_type"] != "kit"), key=lambda s: -s["stock"])[:n]


def make_bill(boot, dev, no, items, pay_mode="cash", when=None, **kw):
    lines = []; calc = []
    for s, q, disc in items:
        g, net, tax = line_calc(s["uom"], s["price"], q, disc, s["tax_pct"])
        lines.append(dict(sku_id=s["id"], qty=q, unit_price_paise=s["price"], discount_paise=disc)); calc.append(dict(gross=g, discount=disc, net=net, tax=tax))
    t = bill_calc(calc); sid = str(uuid.uuid4())
    b = dict(id=sid, idem_key=f"{dev['id']}-{sid}", invoice_no=f"{dev['prefix']}{no:05d}", created_at_device=(when or datetime.now()).replace(microsecond=0).isoformat(),
             lines=lines, payments=[dict(mode=pay_mode, amount_paise=t["total"])], net_paise=t["total"]); b.update(kw)
    return b, t


def grant(client, h, dev_id, n=3):
    r = client.post("/api/pos/series", json=dict(device_id=dev_id, count=n), headers=h); assert r.status_code == 200, r.text
    return r.json()


def test_seed_and_bootstrap(boot, eng):
    assert boot["store"]["code"] == "DFH-BLR-001" and len(boot["devices"]) == 2
    assert len(boot["catalog"]) > 1000 and boot["return_days"] == 10
    loose = pick(boot, "g")[0]; assert loose["tax_pct"] in (5.0, 18.0)
    with eng.connect() as c:
        assert c.execute(text("select count(*) from sale where store_id=21")).scalar() >= 40
        assert c.execute(text("select count(*) from sale s where net_paise != (select sum(amount_paise) from payment p where p.sale_id=s.id)")).scalar() == 0


def test_calc_rounding():
    g, n, t = line_calc("g", 137000, 250, 0, 5.0)
    assert (g, n, t) == (34250, 34250, 1631)
    assert bill_calc([dict(gross=g, discount=0, net=n, tax=t)])["total"] == 34300


def test_roles_and_scope(client, boot):
    assert client.get("/api/pos/bootstrap", headers=tok(client, "hr")).status_code == 403
    assert client.get("/api/pos/bootstrap", headers=tok(client, "warehouse")).status_code == 403
    ceo = client.get("/api/pos/bootstrap", headers=tok(client, "ceo")).json(); assert ceo["stores"] and ceo["store"]["id"]
    other = [d for d in client.get("/api/pos/health", headers=tok(client, "ceo")).json()["devices"] if d["store_id"] != 21][0]
    assert client.post("/api/pos/series", json=dict(device_id=other["id"]), headers=tok(client, "cashier")).status_code == 404


def test_series_and_sync_idempotent(client, lab):
    h, boot = lab["h"], lab["boot"]; dev = boot["devices"][0]
    g = grant(client, h, dev["id"], 4); assert g["end"] - g["start"] == 3
    d = dict(dev, prefix=g["prefix"]); loose, pack = pick(boot, "g")[0], pick(boot, "ea")[0]
    b1, t1 = make_bill(boot, d, g["start"], [(loose, 350, 0), (pack, 2, 0)])
    r = client.post("/api/pos/sync", json=dict(device_id=dev["id"], bills=[b1], queue_depth=1), headers=h).json()
    assert r["results"][0]["status"] in ("accepted", "review")
    again = client.post("/api/pos/sync", json=dict(device_id=dev["id"], bills=[b1], queue_depth=0), headers=h).json()
    assert again["results"][0]["status"] == "duplicate"
    det = client.get("/api/pos/bills/" + b1["invoice_no"], headers=h).json()
    assert det["net_paise"] == t1["total"] and len(det["lines"]) == 2 and sum(p["amount_paise"] for p in det["payments"]) == t1["total"]
    assert det["tax_paise"] == t1["tax"] and det["returnable"] is True



def test_pos_customer_sync_updates_crm_history(client, lab, eng):
    h, boot = lab["h"], lab["boot"]
    dev = boot["devices"][0]
    g = grant(client, h, dev["id"], 2)
    d = dict(dev, prefix=g["prefix"])
    pack = pick(boot, "ea")[0]
    phone = "98" + f"{uuid.uuid4().int % 100000000:08d}"
    with eng.connect() as db:
        assert db.execute(text("select 1 from customer where phone=:p"), {"p": phone}).first() is None
    b, total = make_bill(boot, d, g["start"], [(pack, 1, 0)], when=datetime(2026, 10, 31, 12))
    b["customer_phone"] = phone
    b["consent_wa"] = True
    pushed = client.post("/api/pos/sync", json=dict(device_id=dev["id"], bills=[b]), headers=h).json()["results"][0]
    assert pushed["status"] in ("accepted", "review")
    b2, total2 = make_bill(boot, d, g["start"] + 1, [(pack, 2, 0)], when=datetime(2026, 10, 31, 13))
    b2["customer_phone"] = phone
    second = client.post("/api/pos/sync", json=dict(device_id=dev["id"], bills=[b2]), headers=h).json()["results"][0]
    assert second["status"] in ("accepted", "review")
    with eng.connect() as db:
        customer = db.execute(text("select id,city,first_store_id,orders_n,spend_paise,last_order_on,loyalty_points,consent_wa from customer where phone=:p"), {"p": phone}).first()
        assert customer is not None
        assert customer.city == boot["store"]["city"]
        assert customer.first_store_id == lab["store"]
        assert customer.orders_n == 2 and customer.spend_paise == total["total"] + total2["total"]
        assert str(customer.last_order_on)[:10] == "2026-10-31"
        earned = (total["total"] + total2["total"]) // 5000
        assert customer.loyalty_points == earned and customer.consent_wa
        txns = db.execute(text("select count(*) from loyalty_txn where sale_id in (:s1,:s2)"), {"s1": b["id"], "s2": b2["id"]}).scalar()
        assert txns == 2
    detail = client.get(f"/api/crm/customers/{customer.id}", headers=h)
    assert detail.status_code == 200, detail.text
    pos_rows = [x for x in detail.json()["purchases"] if x["source"] == "pos"]
    assert len(pos_rows) == 2 and {x["invoice_no"] for x in pos_rows} == {b["invoice_no"], b2["invoice_no"]}
    assert detail.json()["months"] and detail.json()["months"][-1]["v"] == total["total"] + total2["total"]
    duplicate = client.post("/api/pos/sync", json=dict(device_id=dev["id"], bills=[b]), headers=h).json()["results"][0]
    assert duplicate["status"] == "duplicate"
    with eng.connect() as db:
        after = db.execute(text("select orders_n,spend_paise,loyalty_points from customer where id=:i"), {"i": customer.id}).first()
        assert tuple(after) == (2, total["total"] + total2["total"], (total["total"] + total2["total"]) // 5000)

def test_sync_rejections(client, lab):
    h, boot = lab["h"], lab["boot"]; dev = boot["devices"][-1]; g = grant(client, h, dev["id"], 6)
    d = dict(dev, prefix=g["prefix"]); pack = pick(boot, "ea")[0]
    def push(b): return client.post("/api/pos/sync", json=dict(device_id=dev["id"], bills=[b]), headers=h).json()["results"][0]
    b, _ = make_bill(boot, d, g["start"], [(pack, 1, 0)]); b["net_paise"] += 5000
    assert "mismatch" in push(b)["reason"]
    b, _ = make_bill(boot, d, g["start"], [(pack, 1, 0)]); b["payments"][0]["amount_paise"] -= 100
    assert "add up" in push(b)["reason"]
    b, _ = make_bill(boot, d, g["end"] + 50, [(pack, 1, 0)]); assert "outside" in push(b)["reason"]
    b, _ = make_bill(boot, d, g["start"], [(pack, 1, 0)], when=datetime.now() + timedelta(days=5)); assert "future" in push(b)["reason"]
    b, _ = make_bill(boot, d, g["start"], [(pack, 1, 0)]); b["lines"][0]["qty"] = 0; assert push(b)["status"] == "rejected"
    ok, _ = make_bill(boot, d, g["start"], [(pack, 1, 0)]); assert push(ok)["status"] in ("accepted", "review")
    reuse, _ = make_bill(boot, d, g["start"], [(pack, 1, 0)]); assert "already used" in push(reuse)["reason"]


def test_review_flag_and_split_payment(client, lab):
    h, boot = lab["h"], lab["boot"]; dev = boot["devices"][0]; g = grant(client, h, dev["id"], 3)
    d = dict(dev, prefix=g["prefix"]); pack = pick(boot, "ea")[0]
    low = dict(pack, price=int(pack["price"] * 0.8)); b, t = make_bill(boot, d, g["start"], [(low, 1, 0)])
    r = client.post("/api/pos/sync", json=dict(device_id=dev["id"], bills=[b]), headers=h).json()["results"][0]
    assert r["status"] == "review" and "below list" in r["reason"]
    s, t2 = make_bill(boot, d, g["start"] + 1, [(pack, 4, 0)]); half = t2["total"] // 200 * 100
    s["payments"] = [dict(mode="cash", amount_paise=half), dict(mode="upi", amount_paise=t2["total"] - half, ref="UPI123")]
    assert client.post("/api/pos/sync", json=dict(device_id=dev["id"], bills=[s]), headers=h).json()["results"][0]["status"] in ("accepted", "review")


def test_returns_flow(client, boot, eng):
    h = tok(client, "cashier"); m = tok(client, "storemgr"); pack = pick(boot, "ea")[0]
    inv = insert_sale(eng, boot, 21, boot["devices"][0]["id"], [(pack, 3)], datetime.now())
    det = client.get("/api/pos/bills/" + inv, headers=h).json(); ln = det["lines"][0]
    body = dict(invoice_no=inv, reason="changed_mind", lines=[dict(sale_line_id=ln["id"], qty=1)])
    assert client.post("/api/pos/returns", json=body, headers=h).status_code == 422
    body["reason"] = "damaged"; body["lines"][0]["qty"] = 4; assert client.post("/api/pos/returns", json=body, headers=h).status_code == 422
    body["lines"][0]["qty"] = 2; r = client.post("/api/pos/returns", json=body, headers=h).json()
    assert r["status"] == "pending" and r["refund_paise"] == (2 * ln["net_paise"] * 2 + 3) // 6
    assert client.post(f"/api/pos/returns/{r['id']}/decision", json=dict(action="approve"), headers=h).status_code == 403
    assert client.post(f"/api/pos/returns/{r['id']}/decision", json=dict(action="approve"), headers=m).json()["status"] == "approved"
    body["lines"][0]["qty"] = 2; assert client.post("/api/pos/returns", json=body, headers=h).status_code == 422
    assert client.get("/api/pos/bills/" + inv, headers=h).json()["lines"][0]["returned"] == 2
    other = insert_sale(eng, boot, 22, 1, [(pack, 1)], datetime.now())
    assert client.get("/api/pos/bills/" + other, headers=h).status_code == 404


def test_return_window(client, boot, eng):
    h = tok(client, "cashier")
    with eng.connect() as c:
        old = c.execute(text("select invoice_no from sale where store_id=21 and business_date<'2026-10-20' limit 1")).scalar()
    ln = client.get("/api/pos/bills/" + old, headers=h).json()
    assert ln["returnable"] is False
    r = client.post("/api/pos/returns", json=dict(invoice_no=old, reason="defective", lines=[dict(sale_line_id=ln["lines"][0]["id"], qty=1)]), headers=h)
    assert r.status_code == 409 and "window" in r.json()["detail"]


def test_day_close_cash(client, lab):
    h, boot = lab["h"], lab["boot"]; day = datetime(2019, 1, 1) + timedelta(days=uuid.uuid4().int % 2500); bd = day.date().isoformat(); sid = lab["store"]
    dev = boot["devices"][-1]; g = grant(client, h, dev["id"], 2)
    d = dict(dev, prefix=g["prefix"]); pack = pick(boot, "ea")[0]
    b, t = make_bill(boot, d, g["start"], [(pack, 2, 0)], when=day.replace(hour=12)); b2, t2 = make_bill(boot, d, g["start"] + 1, [(pack, 1, 0)], pay_mode="upi", when=day.replace(hour=13))
    assert all(x["status"] in ("accepted", "review") for x in client.post("/api/pos/sync", json=dict(device_id=dev["id"], bills=[b, b2]), headers=h).json()["results"])
    assert client.post("/api/pos/cash-drop", json=dict(amount_paise=5000, business_date=bd, store_id=sid), headers=h).status_code == 200
    v = client.get("/api/pos/day-close", params=dict(business_date=bd, store_id=sid), headers=h).json()
    assert v["cash_sales"] == t["total"] and v["drops"] == 5000 and v["by_mode"]["upi"] == t2["total"] and v["bills"] == 2
    exp = 100000 + t["total"] - 5000; body = dict(business_date=bd, store_id=sid, opening_cash_paise=100000, counted_cash_paise=exp - 20000)
    assert client.post("/api/pos/day-close", json=body, headers=h).status_code == 422
    r = client.post("/api/pos/day-close", json=dict(body, note="short, recount tomorrow"), headers=h).json()
    assert r["expected_cash_paise"] == exp and r["variance_paise"] == -20000 and r["status"] == "variance"
    assert client.post("/api/pos/day-close", json=dict(body, counted_cash_paise=0, opening_cash_paise=0), headers=h).status_code == 409
    assert client.post("/api/pos/cash-drop", json=dict(amount_paise=100, business_date=bd, store_id=sid), headers=h).status_code == 409


def test_health_planted_stale(client):
    r = client.get("/api/pos/health", headers=tok(client, "ceo")).json()
    assert r["summary"]["stale"] >= 3 and r["devices"][0]["state"] in ("stale", "never")
    mine = client.get("/api/pos/health", headers=tok(client, "storemgr")).json()
    assert {d["store_id"] for d in mine["devices"]} == {21}
    assert client.get("/api/pos/health", headers=tok(client, "cashier")).status_code == 403


def test_sale_posts_to_ledger_fefo(client, lab, eng):
    h, boot = lab["h"], lab["boot"]; dev = boot["devices"][0]; g = grant(client, h, dev["id"], 2); d = dict(dev, prefix=g["prefix"])
    with eng.connect() as c:
        row = c.execute(text("select b.sku_id, b.location_id from stock_balance b join sku s on s.id=b.sku_id join batch t on t.id=b.batch_id where b.location_id=:l and s.base_uom='ea' and s.procurement_type!='kit' and b.qty>=3 and t.expiry_date>='2026-11-15' limit 1"), dict(l=lab["store"])).fetchone()
        if not row: pytest.skip("no stocked pack in the lab store")
        sku = next(s for s in boot["catalog"] if s["id"] == row[0])
        before = c.execute(text("select sum(qty) from stock_balance where location_id=:l and sku_id=:s"), dict(l=lab["store"], s=sku["id"])).scalar()
    b, t = make_bill(boot, d, g["start"], [(sku, 2, 0)])
    assert client.post("/api/pos/sync", json=dict(device_id=dev["id"], bills=[b]), headers=h).json()["results"][0]["status"] in ("accepted", "review")
    with eng.connect() as c:
        after = c.execute(text("select sum(qty) from stock_balance where location_id=:l and sku_id=:s"), dict(l=lab["store"], s=sku["id"])).scalar()
        led = c.execute(text("select sum(qty), count(*) from stock_ledger where doc_type='sale' and doc_id=:i and movement_type='sale'"), dict(i=b["invoice_no"])).fetchone()
        bt = c.execute(text("select batch_id from sale_line where sale_id=:i"), dict(i=b["id"])).scalar()
    assert before - after == 2 and led[0] == -2 and bt is not None
    again = client.post("/api/pos/sync", json=dict(device_id=dev["id"], bills=[b]), headers=h).json()["results"][0]
    assert again["status"] == "duplicate"
    with eng.connect() as c: assert c.execute(text("select sum(qty) from stock_balance where location_id=:l and sku_id=:s"), dict(l=lab["store"], s=sku["id"])).scalar() == after


def test_sale_without_stock_is_kept_and_flagged(client, lab, eng):
    h, boot = lab["h"], lab["boot"]; dev = boot["devices"][0]; g = grant(client, h, dev["id"], 2); d = dict(dev, prefix=g["prefix"])
    sku = next(s for s in boot["catalog"] if s["uom"] == "ea" and s["stock"] <= 0 and s["price"] > 5000)
    b, t = make_bill(boot, d, g["start"], [(sku, 1, 0)])
    r = client.post("/api/pos/sync", json=dict(device_id=dev["id"], bills=[b]), headers=h).json()["results"][0]
    assert r["status"] == "review" and "short by 1" in r["reason"]
    with eng.connect() as c: assert c.execute(text("select count(*) from stock_balance where qty<0")).scalar() == 0
