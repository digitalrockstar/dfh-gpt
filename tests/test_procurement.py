import os, json, pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from dfh.app import create_app
from dfh.db import get_engine
from dfh.procurement_costs import Rates, components, match, line_minor, allocate

DB = os.getenv("DFH_DB", "sqlite:///data/dfh.db")


@pytest.fixture(scope="module")
def client():
    return TestClient(create_app(DB, os.getenv("DFH_DATA", "data"), serve_frontend=False))


@pytest.fixture(scope="module")
def eng():
    return get_engine(DB)


def q(eng, sql, **p):
    with eng.connect() as c:
        return [tuple(r) for r in c.execute(text(sql), p)]


def tok(c, user="ceo"):
    r = c.post("/api/login", json=dict(username=user, password="demo1234")); assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def get(c, path, user="ceo", **p):
    return c.get("/api/procurement" + path, headers=tok(c, user), params=p)


def post(c, path, body, user="ceo"):
    return c.post("/api/procurement" + path, headers=tok(c, user), json=body)


# ---- rules ----
def test_cost_rules():
    R = Rates()
    assert components(1000_00, 10, "CIF", "NDF", "USA", "sea", True, R) == {"inland": R.inland * 10 * 100}
    c = components(1000_00, 10, "CIF", "NDF", "USA", "sea", False, R)
    assert "freight" not in c and "insurance" not in c and abs(c["duty"] - 1000_00 * .35) < 1
    f = components(1000_00, 10, "FOB", "NDF", "USA", "sea", False, R)
    assert f["freight"] > 0 and f["insurance"] > 0 and f["duty"] > c["duty"]
    assert components(1000_00, 10, "FOB", "NDF", "USA", "air", False, R)["freight"] > 10 * f["freight"] / 2
    a = allocate({"freight": 100, "duty": 200}, [dict(value=300, kg=1), dict(value=100, kg=3)])
    assert abs(sum(a) - 300) < 1e-9 and a[0] == 100 * .25 + 200 * .75


def test_match_rules():
    assert match(1000, 1000, 1000, 0, 1)["state"] == "matched"
    assert match(1000, 1000, 1000, 0, 1)["state"] == "matched" and match(1005, 1000, 1000, 0, 1)["state"] == "matched"
    m = match(1000, 1000, 900, 0, 1); assert m["state"] == "mismatch" and m["variance"] == 100 and "quantity" in m["reason"]
    assert match(1060, 1000, 1000, 0, 1)["reason"] == "Billed above the PO price"
    assert match(1000, 1000, 960, 40, 1)["state"] == "matched"           # credit for rejects clears the difference
    assert match(900, 1000, 1000, 0, 1)["variance"] == -100
    assert match(1000, 1000, 0, 0, 1)["state"] == "awaiting"
    assert line_minor(2500, 986, "g") == 2465 and line_minor(12, 750, "ea") == 9000


# ---- seed ----
def test_seed_integrity(eng):
    assert q(eng, "select count(*) from purchase_order")[0][0] >= 200
    assert q(eng, "select count(*) from purchase_order p where total_minor != (select sum(case s.base_uom when 'g' then round(pl.qty / 1000.0 * pl.price_minor) else pl.qty * pl.price_minor end) from purchase_order_line pl join sku s on s.id=pl.sku_id where pl.po_id=p.id)")[0][0] == 0
    assert q(eng, "select count(*) from grn_line where qty_accepted + qty_rejected != qty_received")[0][0] == 0
    assert q(eng, "select count(*) from grn_line where qty_rejected > 0 and reject_reason is null")[0][0] == 0
    assert q(eng, "select count(*) from shipment where eta < etd")[0][0] == 0
    assert q(eng, "select count(*) from shipment sh join purchase_order p on p.id=sh.po_id where p.status='cancelled'")[0][0] == 0
    assert q(eng, "select count(*) from purchase_order p where p.status='received' and not exists (select 1 from grn where po_id=p.id)")[0][0] == 0
    assert q(eng, "select count(*) from (select po_id from vendor_invoice group by po_id having count(*) > 1)")[0][0] == 0
    assert q(eng, "select count(*) from tax_assumption where assumption_key like 'duty_pct_%' and note like '%ASSUMED%'")[0][0] == 8
    assert q(eng, "select count(*) from vendor_payment pay join vendor_invoice i on i.id=pay.invoice_id where i.status != 'paid'")[0][0] == 0


def test_seed_planted(eng):
    held = q(eng, "select sh.ship_no, l.city from shipment sh join purchase_order p on p.id=sh.po_id join location l on l.id=p.dest_location_id where sh.status='customs' and sh.ata <= '2026-10-24'")
    assert held and held[0][1] == "Chennai"
    f2 = json.loads(q(eng, "select params from demo_finding where code='F2'")[0][0])["vendor_id"]
    assert q(eng, "select count(*) from vendor_invoice i join purchase_order p on p.id=i.po_id where i.vendor_id=:v and i.amount_minor > p.total_minor * 1.05", v=f2)[0][0] >= 2


# ---- API ----
def test_read_endpoints_and_scope(client):
    s = get(client, "/summary").json()
    assert s["open_n"] > 20 and s["late_n"] >= 1 and s["decide_n"] >= 1 and 0.1 < s["landed_premium"] < 0.8
    assert get(client, "/pos", status="open", size=5).json()["total"] == s["open_n"]
    late = get(client, "/shipments", view_="late").json()
    assert late["total"] >= 1 and all(x["late_days"] > 2 for x in late["items"])
    assert get(client, "/shipments", view_="nope").status_code == 422
    w = get(client, "/pos", user="warehouse", status="").json()
    assert w["total"] > 0 and all(x["wh_name"] == "Bengaluru Warehouse 1" for x in w["items"])
    other = [x for x in get(client, "/pos", size=50).json()["items"] if x["wh_name"] != "Bengaluru Warehouse 1"][0]
    assert get(client, f"/pos/{other['id']}", user="warehouse").status_code == 403
    assert get(client, "/summary", user="cashier").status_code == 403 and get(client, "/pos", user="storemgr").status_code == 403


def test_po_detail_and_landed(client):
    rec = [x for x in get(client, "/pos", status="received", size=50).json()["items"] if x["currency"] != "INR"][0]
    d = get(client, f"/pos/{rec['id']}").json()
    assert d["landed"]["basis"] == "actual" and d["landed"]["total"] > d["landed"]["goods"] and d["grns"] and d["events"][-1]["status"] == "delivered"
    assert all(l["landed_unit"] > 0 for l in d["lines"])
    assert d["landed"]["components"].get("duty", 0) > 0 and abs(d["landed"]["premium"] - d["landed"]["extra"] / d["landed"]["goods"]) < 1e-6
    a = [x for x in get(client, "/pos", status="in_transit", size=50).json()["items"] if x["currency"] != "INR" and x["ship_status"] in ("booked", "loaded")]
    if a: assert get(client, f"/pos/{a[0]['id']}").json()["landed"]["basis"] == "estimate"


def test_pricebook_and_compare(client):
    cr = get(client, "/pricebook", only="creep", size=50).json()
    assert cr["total"] >= 5 and all(i["creep"] for i in cr["items"])
    r = get(client, "/compare/10").json(); v = r["vendors"]
    assert [x["rank"] for x in v] == list(range(1, len(v) + 1)) and all(v[i]["landed"] <= v[i + 1]["landed"] for i in range(len(v) - 1))
    assert all(x["landed"] >= x["goods"] for x in v)
    for x in v:
        if x["incoterm"] == "CIF": assert "freight" not in x["components"]
        if x["incoterm"] == "EXW": assert list(x["components"]) == ["inland"]
    assert get(client, "/compare/999999").status_code == 404


def test_vendor_scorecard_flags(client):
    r = get(client, "/vendors").json(); by = {x["name"]: x["flags"] for x in r["items"]}
    allf = [f for x in r["items"] for f in x["flags"]]
    assert "Price creep" in allf and "Short supply" in allf and any(x["flags"] for x in r["items"][:3])
    assert r["items"][0]["flags"]


def test_ct_exceptions_include_procurement(client):
    t = [e["title"] for e in client.get("/api/ct/exceptions", headers=tok(client)).json()]
    assert any("in customs" in x for x in t) and any("do not match" in x for x in t) and any("raised prices" in x for x in t)


def pick_open(eng, big):
    cond = "value_paise > 250000000" if big else "value_paise < 40000000 and value_paise > 1000000"
    return q(eng, f"select id from reorder_suggestion where status='open' and vendor_id is not null and {cond} order by id limit 1")[0][0]


def test_order_to_cash_flow(client, eng):
    sid = pick_open(eng, False)
    assert client.post(f"/api/demand/suggestions/{sid}/decision", headers=tok(client), json=dict(action="approve")).status_code == 200
    assert post(client, "/pos/from-suggestions", dict(suggestion_ids=[sid + 100000])).status_code == 404
    r = post(client, "/pos/from-suggestions", dict(suggestion_ids=[sid]), user="cashier"); assert r.status_code == 403
    r = post(client, "/pos/from-suggestions", dict(suggestion_ids=[sid])); assert r.status_code == 200, r.text
    pid = r.json()["created"][0]["id"]
    assert post(client, "/pos/from-suggestions", dict(suggestion_ids=[sid])).status_code == 409
    assert q(eng, "select status, po_id from reorder_suggestion where id=:i", i=sid)[0] == ("ordered", pid)
    d = get(client, f"/pos/{pid}").json(); assert d["po"]["status"] == "draft" and "approve" in d["actions"] and "receive" not in d["actions"]
    assert post(client, f"/pos/{pid}/grn", dict(lines=[]), user="ceo").status_code in (409, 422)
    assert post(client, f"/pos/{pid}/action", dict(action="confirm")).status_code == 409
    assert post(client, f"/pos/{pid}/action", dict(action="approve")).status_code == 200
    assert post(client, f"/pos/{pid}/action", dict(action="approve")).status_code == 409
    assert post(client, f"/pos/{pid}/action", dict(action="confirm", expected_date="soon")).status_code == 422
    assert post(client, f"/pos/{pid}/action", dict(action="confirm")).status_code == 200
    mode = "road" if d["po"]["vtype"] != "import" else "sea"; port = q(eng, "select id from location where is_port=1 and country='India' limit 1")[0][0]
    bad = post(client, f"/pos/{pid}/shipment", dict(mode="road" if mode == "sea" else "sea", etd="2026-11-01", eta="2026-11-10", port_id=port)); assert bad.status_code == 422
    assert post(client, f"/pos/{pid}/shipment", dict(mode=mode, etd="2026-11-10", eta="2026-11-01", port_id=port)).status_code == 422
    r = post(client, f"/pos/{pid}/shipment", dict(mode=mode, etd="2026-11-02", eta="2026-11-20", port_id=port if mode != "road" else None, container_no="MSKU1234567")); assert r.status_code == 200, r.text
    shid = r.json()["id"]
    flow = get(client, f"/pos/{pid}").json()["flow"]
    assert post(client, f"/shipments/{shid}/event", dict(status="booked")).status_code == 409
    assert post(client, f"/shipments/{shid}/event", dict(status="loaded")).status_code == 200
    assert post(client, f"/shipments/{shid}/event", dict(status="in_transit", eta="2026-11-25")).status_code == 200
    assert get(client, f"/pos/{pid}").json()["po"]["status"] == "in_transit"
    assert post(client, f"/shipments/{shid}/event", dict(status="loaded")).status_code == 409
    for s_ in flow[3:-1]: assert post(client, f"/shipments/{shid}/event", dict(status=s_)).status_code == 200
    ls = get(client, f"/pos/{pid}").json()["lines"]
    over = [dict(sku_id=l["sku_id"], qty_received=l["qty"] * 2, expiry_date="2027-06-30") for l in ls]
    assert post(client, f"/pos/{pid}/grn", dict(lines=over)).status_code == 422
    noexp = [dict(sku_id=l["sku_id"], qty_received=l["qty"]) for l in ls]; assert post(client, f"/pos/{pid}/grn", dict(lines=noexp)).status_code == 422
    stale = [dict(sku_id=l["sku_id"], qty_received=l["qty"], expiry_date="2026-01-01") for l in ls]; assert post(client, f"/pos/{pid}/grn", dict(lines=stale)).status_code == 422
    norsn = [dict(sku_id=ls[0]["sku_id"], qty_received=ls[0]["qty"], qty_rejected=ls[0]["qty"] // 10, expiry_date="2027-06-30")]; assert post(client, f"/pos/{pid}/grn", dict(lines=norsn)).status_code == 422
    sp0 = q(eng, "select coalesce(sum(qty),0) from stock_balance where location_id=:l and sku_id=:s", l=d["po"]["dest_location_id"], s=ls[0]["sku_id"])
    good = [dict(sku_id=l["sku_id"], qty_received=l["qty"], expiry_date="2027-06-30") for l in ls]; good[0]["qty_rejected"] = ls[0]["qty"] // 10; good[0]["reject_reason"] = "Moisture above limit"
    r = post(client, f"/pos/{pid}/grn", dict(lines=good), user="warehouse"); assert r.status_code in (200, 403)
    if r.status_code == 403: r = post(client, f"/pos/{pid}/grn", dict(lines=good), user="procurement")
    assert r.status_code == 200 and r.json()["po_status"] == "received", r.text
    d = get(client, f"/pos/{pid}").json()
    assert d["shipment"]["status"] == "delivered" and d["grns"][0]["lines"][0]["batch_no"] and len(d["debit_notes"]) == 1
    assert q(eng, "select count(*) from batch where source_doc_id=:g", g=r.json()["grn_no"])[0][0] == len(ls)
    if sp0: assert q(eng, "select coalesce(sum(qty),0) from stock_balance where location_id=:l and sku_id=:s", l=d["po"]["dest_location_id"], s=ls[0]["sku_id"])[0][0] > sp0[0][0]
    assert post(client, f"/pos/{pid}/grn", dict(lines=good)).status_code == 409
    # invoice and match
    rec_val = sum(line_minor(l["qty"], l["price_minor"], l["base_uom"]) for l in ls)
    assert post(client, f"/pos/{pid}/invoice", dict(inv_no="T-1", inv_date="2026-10-31", amount=-5)).status_code == 422
    r = post(client, f"/pos/{pid}/invoice", dict(inv_no=f"T-{pid}", inv_date="2026-10-31", amount=rec_val / 100)); assert r.status_code == 200, r.text
    assert post(client, f"/pos/{pid}/invoice", dict(inv_no=f"T-{pid}", inv_date="2026-10-31", amount=1)).status_code == 409
    inv = get(client, f"/pos/{pid}").json()["invoice"]; assert inv["state"] == "matched", inv
    iid = inv["id"]
    assert post(client, f"/invoices/{iid}/decision", dict(action="release"), user="procurement").status_code == 403
    assert post(client, f"/invoices/{iid}/decision", dict(action="hold", note="x")).status_code == 422
    assert post(client, f"/invoices/{iid}/decision", dict(action="release"), user="finance").status_code == 200
    assert post(client, f"/invoices/{iid}/decision", dict(action="hold", note="too late now")).status_code == 409
    assert q(eng, "select count(*) from audit_log where table_name in ('purchase_order','shipment','grn','vendor_invoice') and user_id is not null")[0][0] >= 8


def test_limit_and_cancel_returns_suggestion(client, eng):
    sid = pick_open(eng, True)
    assert client.post(f"/api/demand/suggestions/{sid}/decision", headers=tok(client), json=dict(action="approve")).status_code == 200
    pid = post(client, "/pos/from-suggestions", dict(suggestion_ids=[sid]), user="procurement").json()["created"][0]["id"]
    assert post(client, f"/pos/{pid}/action", dict(action="approve"), user="procurement").status_code == 403
    assert get(client, f"/pos/{pid}", user="procurement").json()["po"]["needs_big"] is True
    assert post(client, f"/pos/{pid}/action", dict(action="approve"), user="finance").status_code == 200
    assert post(client, f"/pos/{pid}/action", dict(action="cancel")).status_code == 422
    assert post(client, f"/pos/{pid}/action", dict(action="cancel", note="Vendor price too high")).status_code == 200
    assert q(eng, "select status, po_id from reorder_suggestion where id=:i", i=sid)[0] == ("approved", None)
    assert post(client, f"/pos/{pid}/action", dict(action="cancel", note="again")).status_code == 409


def test_invoice_hold_and_dispute(client, eng):
    rows = get(client, "/match", state="decide", size=50).json()["items"]
    pend = [r for r in rows if r["status"] == "pending" and r["variance"] > 0]
    if len(pend) < 2: pytest.skip("seeded mismatches already decided in an earlier run")
    a, b = pend[0], pend[1]
    assert post(client, f"/invoices/{a['id']}/decision", dict(action="release"), user="finance").status_code == 422     # mismatch needs a reason
    assert post(client, f"/invoices/{a['id']}/decision", dict(action="hold", note="Waiting for vendor credit note")).status_code == 200
    n0 = q(eng, "select count(*) from debit_note")[0][0]
    assert post(client, f"/invoices/{b['id']}/decision", dict(action="dispute", note="Billed above PO price")).status_code == 200
    assert q(eng, "select count(*) from debit_note")[0][0] == n0 + 1
    assert post(client, f"/invoices/{b['id']}/decision", dict(action="dispute", note="again please")).status_code == 409
    c = get(client, "/match", state="all", size=100).json()
    assert all(r["id"] != b["id"] for r in c["items"])


def test_landed_actuals(client, eng):
    sh = q(eng, "select sh.id from shipment sh join landed_cost l on l.shipment_id=sh.id where l.component='duty' limit 1")[0][0]
    assert post(client, f"/shipments/{sh}/landed", dict(components={"bogus": 5})).status_code == 422
    assert post(client, f"/shipments/{sh}/landed", dict(components={"duty": -5})).status_code == 422
    assert post(client, f"/shipments/{sh}/landed", dict(components={"clearing": 12345.5}), user="warehouse").status_code == 403
    assert post(client, f"/shipments/{sh}/landed", dict(components={"clearing": 12345.5})).status_code == 200
    assert q(eng, "select amount_paise from landed_cost where shipment_id=:s and component='clearing'", s=sh) == [(1234550,)]
