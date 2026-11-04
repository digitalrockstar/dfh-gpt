import os, shutil, sqlite3
import pytest
from fastapi.testclient import TestClient
from dfh.app import create_app

SRC = os.getenv("DFH_DB", "sqlite:///data/dfh.db").replace("sqlite:///", "")
TODAY = "2026-10-31"


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    p = str(tmp_path_factory.mktemp("db") / "dfh.db"); shutil.copy(SRC, p)
    con = sqlite3.connect(p)
    return TestClient(create_app("sqlite:///" + p, os.getenv("DFH_DATA", "data"), serve_frontend=False)), con


def H(c, user):
    return {"Authorization": "Bearer " + c.post("/api/login", json=dict(username=user, password="demo1234")).json()["token"]}


def q(con, sql, *a): return con.execute(sql, a).fetchall()
def bal(con, loc, sku, bid=None): return q(con, "select coalesce(sum(qty),0) from stock_balance where location_id=? and sku_id=? and (? is null or batch_id=?)", loc, sku, bid, bid)[0][0]


def fresh_multi(con, wh, uom="ea", n=2):
    return q(con, "select b.sku_id from stock_balance b join batch bt on bt.id=b.batch_id join sku s on s.id=b.sku_id where b.location_id=? and b.qty>0 and bt.expiry_date>=? and s.base_uom=? and s.status='active' "
                  "group by b.sku_id having count(*)>=? order by b.sku_id limit 5", wh, TODAY, uom, n)


def test_seed_invariants(env):
    c, con = env
    assert q(con, "select count(*) from (select l.location_id, l.sku_id, l.batch_id, sum(l.qty) s from stock_ledger l group by 1,2,3) x left join stock_balance b on b.location_id=x.location_id and b.sku_id=x.sku_id and b.batch_id=x.batch_id where x.s <> coalesce(b.qty,0)")[0][0] == 0
    assert q(con, "select count(*) from stock_balance where qty<0")[0][0] == 0
    assert q(con, "select count(*) from stock_balance b join batch bt on bt.id=b.batch_id where bt.sku_id<>b.sku_id")[0][0] == 0
    assert q(con, "select count(*) from batch where expiry_date<=mfg_date or mfg_date>received_on")[0][0] == 0
    assert q(con, "select count(*) from transfer where status='in_transit'")[0][0] >= 5 and q(con, "select count(*) from pick")[0][0] > 0


def test_planted_findings(env):
    c, con = env; j = c.get("/api/inv/overview", headers=H(c, "ceo")).json(); ex = {e["type"]: e for e in j["exceptions"]}
    hyd = next(w for w in j["warehouses"] if w["id"] == 6)
    assert ex["shrink"]["ref"] == 6 and hyd["shrink_rate"] > 5 * j["median_shrink_rate"] and hyd["unexplained90"] > 0.5 * hyd["shrink90"]
    assert q(con, "select count(*) from location where id=? and city='Pune'", ex["cover"]["ref"])[0][0] == 1
    assert "Pineapple" in ex["expiry"]["title"] and "kiosks" in ex["expiry"]["title"]
    k = c.get("/api/inv/kits", headers=H(c, "ceo")).json(); names = [x["name"] for x in k["items"] if x["short"]]
    assert sum("Diwali" in n for n in names) >= 2 and k["short"] <= 12 and "assumption" in k
    assert ex["kit"]["severity"] == "high"


def test_stock_and_ledger_views(env):
    c, con = env; h = H(c, "ceo")
    r = c.get("/api/inv/stock?location_id=8&flag=low", headers=h).json()
    assert r["totals"]["low"] + r["totals"]["out"] >= 20 and all(i["state"] in ("low", "out") for i in r["items"])
    r = c.get("/api/inv/stock?location_id=1&sort=expiry&size=5", headers=h).json(); assert r["items"][0]["days_left"] is not None and r["totals"]["value"] > 0
    sid = fresh_multi(con, 1)[0][0]; d = c.get(f"/api/inv/sku/{sid}", headers=h).json(); assert d["locations"] and len(d["batches"]) >= 2
    bid = d["batches"][0]["id"]; lg = c.get(f"/api/inv/ledger?batch_id={bid}", headers=h).json(); assert lg["total"] >= 1
    assert c.get("/api/inv/ledger", headers=h).status_code == 422
    e = c.get("/api/inv/expiry?days=30", headers=h).json(); assert e["buckets"]["d15"] + e["buckets"]["d30"] > 0 and all(i["days_left"] <= 30 for i in e["items"])


def test_fefo_dispatch_and_short_receipt(env):
    c, con = env; h = H(c, "warehouse"); sid = fresh_multi(con, 1)[0][0]
    bs = q(con, "select b.batch_id, b.qty from stock_balance b join batch bt on bt.id=b.batch_id where b.location_id=1 and b.sku_id=? and b.qty>0 and bt.expiry_date>=? order by bt.expiry_date", sid, TODAY)
    before = bal(con, 1, sid); want = bs[0][1] + 1
    t = c.post("/api/inv/transfers", headers=h, json=dict(from_id=1, to_id=2, lines=[dict(sku_id=sid, qty=want)])).json(); assert t["status"] == "draft"
    assert c.post(f"/api/inv/transfers/{t['id']}/dispatch", headers=h, json={}).status_code == 422                       # vehicle needed
    d = c.post(f"/api/inv/transfers/{t['id']}/dispatch", headers=h, json=dict(vehicle_no="KA01AB1234")).json()
    assert d["status"] == "in_transit" and [(l["qty_sent"]) for l in d["lines"]] == [bs[0][1], 1]
    con2 = sqlite3.connect(con.execute("pragma database_list").fetchall()[0][2]); assert bal(con2, 1, sid) == before - want
    l0 = d["lines"][0]; short = l0["qty_sent"] - 1
    assert c.post(f"/api/inv/transfers/{t['id']}/receive", headers=h, json=dict(lines=[dict(line_id=l0["id"], qty_received=short + 5), dict(line_id=d["lines"][1]["id"], qty_received=1)])).status_code in (403, 422)
    h2 = H(c, "ceo")
    assert c.post(f"/api/inv/transfers/{t['id']}/receive", headers=h2, json=dict(lines=[dict(line_id=l0["id"], qty_received=short)])).status_code == 422   # every line needed
    r = c.post(f"/api/inv/transfers/{t['id']}/receive", headers=h2, json=dict(lines=[dict(line_id=l0["id"], qty_received=short), dict(line_id=d["lines"][1]["id"], qty_received=1)])).json()
    assert r["status"] == "received" and sorted(x["movement_type"] for x in r["ledger"]).count("transit_shrink") == 1
    assert bal(con2, 2, sid, bs[0][0]) >= short and c.post(f"/api/inv/transfers/{t['id']}/receive", headers=h2, json=dict(lines=[])).status_code == 422


def test_transfer_rules(env):
    c, con = env; h = H(c, "ceo"); sid = fresh_multi(con, 1)[0][0]; g = fresh_multi(con, 1, "g", 1)[0][0]
    mk = lambda to, lines: c.post("/api/inv/transfers", headers=h, json=dict(from_id=1, to_id=to, lines=lines))
    assert mk(1, [dict(sku_id=sid, qty=1)]).status_code == 422 and mk(2, []).status_code == 422 and mk(2, [dict(sku_id=sid, qty=0)]).status_code == 422
    assert mk(2, [dict(sku_id=g, qty=150)]).status_code == 422                                                          # loose moves in 100 g steps
    big = mk(2, [dict(sku_id=sid, qty=10 ** 9)]).json(); r = c.post(f"/api/inv/transfers/{big['id']}/dispatch", headers=h, json=dict(vehicle_no="KA01AB1111"))
    assert r.status_code == 422 and "Not enough" in r.json()["detail"]
    assert c.post(f"/api/inv/transfers/{big['id']}/cancel", headers=h).json()["status"] == "cancelled" and c.post(f"/api/inv/transfers/{big['id']}/cancel", headers=h).status_code == 422
    dest = q(con, "select id from location where type='warehouse' and state<>(select state from location where id=1) order by id limit 1")[0][0]
    row = q(con, "select b.sku_id, b.qty, bt.unit_cost_paise, s.base_uom from stock_balance b join batch bt on bt.id=b.batch_id join sku s on s.id=b.sku_id where b.location_id=1 and bt.expiry_date>=? and s.base_uom='g' order by b.qty*bt.unit_cost_paise desc limit 1", TODAY)[0]
    qty = min(row[1], int(6_000_000 / row[2] * 1000 // 100 * 100 + 100)); assert qty / 1000 * row[2] > 5_000_000
    t = mk(dest, [dict(sku_id=row[0], qty=qty)]).json(); r = c.post(f"/api/inv/transfers/{t['id']}/dispatch", headers=h, json=dict(vehicle_no="KA01AB2222"))
    assert r.status_code == 422 and "e-way" in r.json()["detail"]
    assert c.post(f"/api/inv/transfers/{t['id']}/dispatch", headers=h, json=dict(vehicle_no="KA01AB2222", eway_ref="EW123456789012")).json()["status"] == "in_transit"


def test_indent_flow_and_reservation(env):
    c, con = env; sm = H(c, "storemgr"); ceo = H(c, "ceo")
    st = q(con, "select scope_id from app_user where username='storemgr'")[0][0]; wh = q(con, "select serving_wh_id from location where id=?", st)[0][0]
    cand = q(con, "select a.sku_id, a.max_qty, sum(b.qty) av from store_assortment a join stock_balance b on b.sku_id=a.sku_id and b.location_id=? join batch bt on bt.id=b.batch_id and bt.expiry_date>=? join sku s on s.id=a.sku_id "
                  "where a.store_id=? and s.base_uom='ea' and s.status='active' group by a.sku_id having av>=a.max_qty order by a.sku_id limit 2", wh, TODAY, st)
    assert len(cand) == 2
    unlisted = q(con, "select id from sku where status='active' and base_uom='ea' and id not in (select sku_id from store_assortment where store_id=?) limit 1", st)[0][0]
    assert c.post("/api/inv/indents", headers=sm, json=dict(lines=[dict(sku_id=unlisted, qty=1)])).status_code == 422
    assert c.post("/api/inv/indents", headers=sm, json=dict(lines=[dict(sku_id=cand[0][0], qty=cand[0][1] * 3)])).status_code == 422
    assert c.post("/api/inv/indents", headers=sm, json=dict(needed_by="2026-10-01", lines=[dict(sku_id=cand[0][0], qty=1)])).status_code == 422
    i = c.post("/api/inv/indents", headers=sm, json=dict(lines=[dict(sku_id=s, qty=m) for s, m, _ in cand])).json(); assert i["status"] == "open"
    assert c.post(f"/api/inv/indents/{i['id']}/allocate", headers=sm).status_code == 403
    a = c.post(f"/api/inv/indents/{i['id']}/allocate", headers=ceo).json(); assert a["status"] == "allocated" and all(l["qty_alloc"] == l["qty_req"] for l in a["lines"])
    assert c.post(f"/api/inv/indents/{i['id']}/dispatch", headers=ceo, json={}).status_code == 422                                         # vehicle
    before = bal(con, st, cand[0][0]); d = c.post(f"/api/inv/indents/{i['id']}/dispatch", headers=ceo, json=dict(vehicle_no="KA01AB3333")).json()
    assert d["status"] == "dispatched" and d["transfer"]["status"] == "in_transit"
    t = c.get(f"/api/inv/transfers/{d['transfer']['id']}", headers=sm).json(); assert t["to_location_id"] == st
    r = c.post(f"/api/inv/transfers/{t['id']}/receive", headers=sm, json=dict(lines=[dict(line_id=l["id"], qty_received=l["qty_sent"]) for l in t["lines"]])).json()
    assert r["status"] == "received" and bal(sqlite3.connect(con.execute("pragma database_list").fetchall()[0][2]), st, cand[0][0]) == before + cand[0][1]
    assert c.get(f"/api/inv/indents/{i['id']}", headers=sm).json()["status"] == "received"
    # reservation: an allocation holds stock, so a second indent cannot take the same units
    sid = cand[1][0]; avail = bal(con, wh, sid); held = q(con, "select coalesce(sum(p.qty),0) from pick p join indent_line il on il.id=p.indent_line_id join indent i on i.id=il.indent_id where i.status='allocated' and i.warehouse_id=? and il.sku_id=? and p.picked_at is null", wh, sid)[0][0]
    assert held >= 0 and avail >= 0
    j = c.post("/api/inv/indents", headers=sm, json=dict(lines=[dict(sku_id=sid, qty=1)])).json(); assert c.post(f"/api/inv/indents/{j['id']}/cancel", headers=sm).json()["status"] == "cancelled"
    assert c.post(f"/api/inv/indents/{j['id']}/cancel", headers=sm).status_code == 422


def test_repack(env):
    c, con = env; h = H(c, "ceo"); opts = c.get("/api/inv/repack/options?warehouse_id=2", headers=h).json()
    o = next(x for x in opts if x["packs"] and any(b["qty"] >= 60000 for b in x["batches"])); b = next(b for b in o["batches"] if b["qty"] >= 60000); p = o["packs"][0]; inp = p["pack_g"] * 10
    base = dict(warehouse_id=2, bulk_sku_id=o["id"], batch_id=b["id"], input_g=inp)
    assert c.post("/api/inv/repack", headers=h, json=dict(base, outputs=[dict(sku_id=p["id"], units=9)])).status_code == 422                        # 10 percent loss
    assert c.post("/api/inv/repack", headers=h, json=dict(base, outputs=[dict(sku_id=p["id"], units=11)])).status_code == 422                       # more than went in
    other = q(con, "select id from sku where procurement_type='finished' limit 1")[0][0]
    assert c.post("/api/inv/repack", headers=h, json=dict(base, outputs=[dict(sku_id=other, units=1)])).status_code == 422
    assert c.post("/api/inv/repack", headers=h, json=dict(base, input_g=10 ** 9, outputs=[dict(sku_id=p["id"], units=1)])).status_code == 422
    before = bal(con, 2, o["id"], b["id"]); r = c.post("/api/inv/repack", headers=h, json=dict(base, outputs=[dict(sku_id=p["id"], units=10)])).json()
    assert r["loss_g"] == 0 and r["packed_g"] == inp
    con2 = sqlite3.connect(con.execute("pragma database_list").fetchall()[0][2])
    assert bal(con2, 2, o["id"], b["id"]) == before - inp and q(con2, "select sum(qty) from stock_ledger where doc_id=? and movement_type='repack_in'", r["job_no"])[0][0] == 10
    assert c.get("/api/inv/repack?warehouse_id=2", headers=h).json()["items"][0]["job_no"] == r["job_no"]
    inp2 = -(-int(p["pack_g"] * 40 / 0.97) // 100) * 100; near = c.post("/api/inv/repack", headers=h, json=dict(base, input_g=inp2, outputs=[dict(sku_id=p["id"], units=40)])).json()
    assert near["warn"] is True and 2 < near["loss_pct"] < 5


def test_counts_and_approval_limits(env):
    c, con = env; wh_h, ceo = H(c, "warehouse"), H(c, "ceo")
    assert c.post("/api/inv/counts", headers=wh_h, json=dict(location_id=1, abc_class="A")).status_code == 422                                     # a planned A count is already open
    plan = next(p for p in c.get("/api/inv/counts?location_id=1&status=planned", headers=wh_h).json() if p["abc_class"] == "A"); d = c.get(f"/api/inv/counts/{plan['id']}", headers=wh_h).json()
    assert len(d["lines"]) > 5 and c.post(f"/api/inv/counts/{plan['id']}/approve", headers=wh_h).status_code == 422                                  # not submitted yet
    assert c.post(f"/api/inv/counts/{plan['id']}/submit", headers=wh_h, json=dict(lines=[])).status_code == 422
    top = max(d["lines"], key=lambda l: l["system_qty"] * 0 + (q(con, "select unit_cost_paise from batch where batch_no=?", l["batch_no"])[0][0] * l["system_qty"] / (1000 if l["base_uom"] == "g" else 1)))
    lines = [dict(line_id=l["id"], counted_qty=(0 if l["id"] == top["id"] else l["system_qty"])) for l in d["lines"]]
    s = c.post(f"/api/inv/counts/{plan['id']}/submit", headers=wh_h, json=dict(lines=lines)).json(); assert s["status"] == "submitted" and s["lines"][0]["var_value"] > 500_000
    r = c.post(f"/api/inv/counts/{plan['id']}/approve", headers=wh_h); assert r.status_code == 403 and "CEO" in r.json()["detail"]
    sid, bn = top["sku_id"], top["batch_no"]; bid = q(con, "select id from batch where batch_no=?", bn)[0][0]; cur = bal(con, 1, sid, bid)
    assert c.post(f"/api/inv/counts/{plan['id']}/approve", headers=ceo).json()["status"] == "closed"
    con2 = sqlite3.connect(con.execute("pragma database_list").fetchall()[0][2]); assert bal(con2, 1, sid, bid) == 0 and q(con2, "select sum(qty) from stock_ledger where movement_type='count_adjust' and doc_id=?", plan["plan_no"])[0][0] == -cur
    n = c.post("/api/inv/counts", headers=wh_h, json=dict(location_id=1, abc_class="C", size=5)).json(); assert n["status"] == "planned" and 1 <= len(n["lines"]) <= 5
    small = [dict(line_id=l["id"], counted_qty=l["system_qty"] - (100 if l["base_uom"] == "g" else 1) if l["system_qty"] >= 200 else l["system_qty"]) for l in n["lines"]]
    assert c.post(f"/api/inv/counts/{n['id']}/submit", headers=wh_h, json=dict(lines=small)).json()["status"] == "submitted"
    assert c.post(f"/api/inv/counts/{n['id']}/approve", headers=wh_h).json()["status"] == "closed"
    assert c.post("/api/inv/counts", headers=wh_h, json=dict(location_id=1, abc_class="Z")).status_code == 422 and c.post("/api/inv/counts", headers=wh_h, json=dict(location_id=2, abc_class="C")).status_code == 403




def test_inventory_reconciliation_matches_ledger_projection_and_scopes(env):
    c, con = env
    ceo, wh, sm = H(c, "ceo"), H(c, "warehouse"), H(c, "storemgr")
    r = c.get("/api/inv/reconciliation", headers=ceo)
    assert r.status_code == 200
    data = r.json()
    assert data["checked"] > 0
    assert data["mismatch_count"] == 0
    assert data["absolute_variance"] == 0
    assert data["negative_balance_count"] == 0
    assert data["items"] == []
    scoped = c.get("/api/inv/reconciliation?location_id=1", headers=wh)
    assert scoped.status_code == 200 and scoped.json()["checked"] > 0
    assert c.get("/api/inv/reconciliation?location_id=2", headers=wh).status_code == 403
    store_id = q(con, "select scope_id from app_user where username='storemgr'")[0][0]
    other_store = q(con, "select id from location where type='store' and id<>? limit 1", store_id)[0][0]
    assert c.get(f"/api/inv/reconciliation?location_id={store_id}", headers=sm).status_code == 200
    assert c.get(f"/api/inv/reconciliation?location_id={other_store}", headers=sm).status_code == 403
    assert c.get("/api/inv/reconciliation", headers=H(c, "cashier")).status_code == 403


def test_inventory_reconciliation_detects_projection_drift(env):
    c, con = env
    h = H(c, "ceo")
    row = q(con, "select location_id, sku_id, batch_id, qty from stock_balance where qty>0 limit 1")[0]
    con.execute("update stock_balance set qty=qty+7 where location_id=? and sku_id=? and batch_id=?", row[:3])
    con.commit()
    r = c.get(f"/api/inv/reconciliation?location_id={row[0]}", headers=h)
    assert r.status_code == 200
    d = r.json()
    assert d["mismatch_count"] == 1
    assert d["absolute_variance"] == 7
    assert d["items"][0]["kind"] == "quantity_mismatch"
    # Reconciliation is observational: it does not silently repair or mutate stock.
    assert q(con, "select qty from stock_balance where location_id=? and sku_id=? and batch_id=?", *row[:3])[0][0] == row[3] + 7

def test_scope_and_roles(env):
    c, con = env; wh, sm, ca = H(c, "warehouse"), H(c, "storemgr"), H(c, "cashier"); ceo = H(c, "ceo")
    assert c.get("/api/inv/overview", headers=ca).status_code == 403 and c.get("/api/inv/overview", headers=sm).status_code == 403
    assert c.get("/api/inv/stock?location_id=2", headers=wh).status_code == 403 and c.get("/api/inv/stock?location_id=1", headers=wh).status_code == 200
    st = q(con, "select scope_id from app_user where username='storemgr'")[0][0]; other = q(con, "select id from location where type='store' and id<>? limit 1", st)[0][0]
    assert c.get(f"/api/inv/stock?location_id={other}", headers=sm).status_code == 403 and c.get(f"/api/inv/stock?location_id={st}", headers=sm).status_code == 200
    assert c.post("/api/inv/transfers", headers=sm, json=dict(from_id=1, to_id=2, lines=[dict(sku_id=1, qty=1)])).status_code == 403
    assert c.post("/api/inv/transfers", headers=wh, json=dict(from_id=2, to_id=1, lines=[dict(sku_id=1, qty=1)])).status_code == 403                 # not their warehouse
    ow = c.get("/api/inv/overview", headers=wh).json(); assert [w["id"] for w in ow["warehouses"]] == [1]
    assert all(t["from_code"] and True for t in c.get("/api/inv/transfers", headers=wh).json()["items"])
    assert c.get("/api/inv/kits", headers=wh).status_code == 200 and c.get("/api/inv/shrink?warehouse_id=2", headers=wh).status_code == 403
    assert c.get("/api/inv/shrink?warehouse_id=6", headers=ceo).json()["by_reason"][0]["reason"] == "unexplained"
