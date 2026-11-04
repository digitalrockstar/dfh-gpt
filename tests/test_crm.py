import os, shutil, pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from dfh.app import create_app
from dfh.db import get_engine

SRC = os.getenv("DFH_DB", "sqlite:///data/dfh.db").replace("sqlite:///", "")


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    p = str(tmp_path_factory.mktemp("db") / "dfh.db"); shutil.copy(SRC, p)
    url = "sqlite:///" + p
    return TestClient(create_app(url, os.getenv("DFH_DATA", "data"), serve_frontend=False)), get_engine(url)


def tok(c, user="ceo"):
    r = c.post("/api/login", json=dict(username=user, password="demo1234")); assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def q(eng, sql, **p):
    with eng.connect() as c: return c.execute(text(sql), p).fetchall()


def test_seed_shape(env):
    c, e = env
    assert q(e, "select count(*) from customer where orders_n != (select count(*) from customer_purchase p where p.customer_id=customer.id)")[0][0] == 0
    assert q(e, "select count(*) from customer where loyalty_points != coalesce((select sum(points) from loyalty_txn t where t.customer_id=customer.id),0)")[0][0] == 0
    assert q(e, "select count(*) from customer where loyalty_points < 0")[0][0] == 0
    assert q(e, "select count(*) from customer where consent_wa=0 and last_marketing_at is not null")[0][0] == 0
    assert {r[0] for r in q(e, "select distinct segment from customer")} == {"New", "Regular", "Loyal", "Lapsing", "One-time"}
    assert q(e, "select count(*) from wa_order where status='pay_pending' and created_at < '2026-10-30 09:00:00'")[0][0] >= 14      # planted: unpaid for over a day
    assert q(e, "select count(*) from wa_order o where total_paise != (select sum(qty*price_paise) from wa_order_line l where l.order_id=o.id) + delivery_paise")[0][0] == 0
    assert q(e, "select count(*) from quote where status='sent' and valid_to between '2026-10-31' and '2026-11-03'")[0][0] >= 3      # planted: quotes about to expire
    assert q(e, "select max(optouts*1.0/sent) from campaign where sent>0")[0][0] > 0.03                                          # planted: broad blast with high opt-outs


def test_roles_scope_and_masking(env):
    c, e = env
    assert c.get("/api/crm/summary", headers=tok(c, "cashier")).status_code == 403
    assert c.get("/api/crm/summary", headers=tok(c, "finance")).status_code == 403
    assert c.get("/api/crm/campaigns", headers=tok(c, "storemgr")).status_code == 403
    assert c.get("/api/crm/quotes", headers=tok(c, "storemgr")).status_code == 403
    ceo = c.get("/api/crm/customers?size=5", headers=tok(c)).json()["items"][0]
    assert "•" not in ceo["phone"]
    mk = c.get("/api/crm/customers?size=5", headers=tok(c, "marketing")).json()["items"][0]
    assert "•" in mk["phone"]
    sm = c.get("/api/crm/customers?size=100", headers=tok(c, "storemgr")).json()
    assert sm["total"] > 0 and all(i["phone"].count("•") for i in sm["items"])
    sc = c.get("/api/me", headers=tok(c, "storemgr")).json()["scope_id"]
    ids = [i["id"] for i in sm["items"]]
    assert all(c.get(f"/api/crm/customers/{i}", headers=tok(c, "storemgr")).status_code == 200 for i in ids[:3])
    row = c.get("/api/crm/customers/%d" % ids[0], headers=tok(c)).json()["customer"]
    linked = q(e, "select 1 from sale where customer_id=:i and store_id=:s union all select 1 from customer_purchase where customer_id=:i and store_id=:s limit 1", i=row["id"], s=sc)
    assert row["first_store_id"] == sc or bool(linked)
    foreign = q(e, "select c.id from customer c where c.first_store_id != :s and c.id not in (select customer_id from sale where store_id=:s and customer_id is not null) and c.id not in (select customer_id from customer_purchase where store_id=:s) limit 1", s=sc)[0][0]
    assert c.get(f"/api/crm/customers/{foreign}", headers=tok(c, "storemgr")).status_code == 404


def test_search_and_detail(env):
    c, e = env; h = tok(c)
    code = q(e, "select code from customer where id=77")[0][0]
    r = c.get(f"/api/crm/customers?q={code}", headers=h).json(); assert r["total"] == 1 and r["items"][0]["id"] == 77
    d = c.get("/api/crm/customers/77", headers=h).json()
    assert d["customer"]["points_value"] == d["customer"]["loyalty_points"] * 50
    assert sum(m["n"] for m in d["months"]) == d["customer"]["orders_n"]
    s = c.get("/api/crm/summary", headers=h).json()
    assert s["liability"] == s["points"] * 50 and s["customers"] == 25000
    assert sum(x["n"] for x in s["segments"]) == 25000


def test_consent_rules(env):
    c, e = env; h = tok(c)
    cid = q(e, "select id from customer where consent_wa=1 limit 1")[0][0]
    assert c.post(f"/api/crm/customers/{cid}/consent", headers=h, json=dict(action="grant", source="counter")).status_code == 422      # already opted in
    assert c.post(f"/api/crm/customers/{cid}/consent", headers=h, json=dict(action="revoke", source="counter")).status_code == 422     # wrong source for revoke
    assert c.post(f"/api/crm/customers/{cid}/consent", headers=h, json=dict(action="revoke", source="stop_reply")).status_code == 200
    assert q(e, "select consent_wa from customer where id=:i", i=cid)[0][0] == 0
    assert c.post(f"/api/crm/customers/{cid}/consent", headers=h, json=dict(action="revoke", source="request")).status_code == 422
    assert q(e, "select count(*) from audit_log where action='crm.consent.revoke' and row_id=:i", i=str(cid))[0][0] == 1
    assert c.post(f"/api/crm/customers/{cid}/consent", headers=h, json=dict(action="grant", source="whatsapp_optin")).status_code == 200


def test_loyalty_rules(env):
    c, e = env; mk = tok(c, "marketing")
    cid = q(e, "select id from customer where loyalty_points between 100 and 400 limit 1")[0][0]; bal = q(e, "select loyalty_points from customer where id=:i", i=cid)[0][0]
    assert c.post(f"/api/crm/customers/{cid}/loyalty", headers=mk, json=dict(points=600, reason="goodwill gesture")).status_code == 403
    assert c.post(f"/api/crm/customers/{cid}/loyalty", headers=mk, json=dict(points=-(bal + 1), reason="correction")).status_code == 422
    assert c.post(f"/api/crm/customers/{cid}/loyalty", headers=mk, json=dict(points=50, reason="x")).status_code == 422
    assert c.post(f"/api/crm/customers/{cid}/loyalty", headers=tok(c, "storemgr"), json=dict(points=50, reason="goodwill gesture")).status_code == 403
    r = c.post(f"/api/crm/customers/{cid}/loyalty", headers=mk, json=dict(points=50, reason="Late bill added")); assert r.status_code == 200 and r.json()["points"] == bal + 50
    assert c.post(f"/api/crm/customers/{cid}/loyalty", headers=tok(c), json=dict(points=600, reason="Service recovery")).status_code == 200
    assert q(e, "select loyalty_points from customer where id=:i", i=cid)[0][0] == q(e, "select sum(points) from loyalty_txn where customer_id=:i", i=cid)[0][0]


def stocked(e):
    r = q(e, "select b.location_id, b.sku_id, sum(b.qty) from stock_balance b join sku s on s.id=b.sku_id join batch t on t.id=b.batch_id join location l on l.id=b.location_id where l.type='store' and l.id!=21 and s.base_uom='ea' and s.status='active' "
             "and s.mrp_paise is not null and t.expiry_date>='2026-10-31' group by 1,2 having sum(b.qty)>=12 limit 1")[0]
    return r[0], r[1], r[2]


def test_order_flow_posts_stock(env):
    c, e = env; h = tok(c); loc, sku, have = stocked(e)
    body = dict(phone="+91-9000011122", name="Test Buyer", lines=[dict(sku_id=sku, qty=3)], fulfil_location_id=loc)
    r = c.post("/api/crm/orders", headers=h, json=body); assert r.status_code == 200, r.text
    oid, no = r.json()["id"], r.json()["order_no"]
    mrp = q(e, "select mrp_paise from sku where id=:i", i=sku)[0][0]; assert r.json()["total_paise"] == 3 * mrp + (0 if 3 * mrp >= 99900 else 4900)
    A = lambda a, **k: c.post(f"/api/crm/orders/{oid}/action", headers=h, json=dict(action=a, **k))
    assert A("pack").status_code == 422 and A("dispatch").status_code == 422                 # cannot skip steps
    assert A("confirm").status_code == 200 and A("send_link").status_code == 200
    assert "pay.dfh-demo" in c.get(f"/api/crm/orders/{oid}", headers=h).json()["order"]["payment_link"]
    assert A("dispatch").status_code == 422
    assert A("mark_paid").status_code == 200 and A("pack").status_code == 200
    before = q(e, "select coalesce(sum(qty),0) from stock_balance where location_id=:l and sku_id=:s", l=loc, s=sku)[0][0]
    assert A("dispatch").status_code == 200
    assert q(e, "select coalesce(sum(qty),0) from stock_balance where location_id=:l and sku_id=:s", l=loc, s=sku)[0][0] == before - 3
    assert q(e, "select count(*) from stock_ledger where doc_type='wa_order' and doc_id=:d", d=no)[0][0] >= 1
    assert A("cancel", note="changed my mind").status_code == 422                            # dispatched orders cannot be cancelled
    assert A("deliver").status_code == 200
    kinds = [m["template"] for m in c.get(f"/api/crm/orders/{oid}", headers=h).json()["messages"]]
    assert kinds == ["order_confirm", "payment_link", "dispatch_update"]
    assert q(e, "select consent_wa from customer where phone='+91-9000011122'")[0][0] == 0   # a new WhatsApp customer is not opted in to marketing


def test_cancel_after_payment_flags_refund(env):
    c, e = env; h = tok(c); loc, sku, _ = stocked(e)
    oid = c.post("/api/crm/orders", headers=h, json=dict(phone="+91-9000011123", lines=[dict(sku_id=sku, qty=1)], fulfil_location_id=loc)).json()["id"]
    for a in ("confirm", "send_link", "mark_paid"): assert c.post(f"/api/crm/orders/{oid}/action", headers=h, json=dict(action=a)).status_code == 200
    assert c.post(f"/api/crm/orders/{oid}/action", headers=h, json=dict(action="cancel")).status_code == 422                     # reason needed
    r = c.post(f"/api/crm/orders/{oid}/action", headers=h, json=dict(action="cancel", note="Customer asked to cancel")); assert r.status_code == 200 and r.json()["refund_due"]
    assert "Refund" in c.get(f"/api/crm/orders/{oid}", headers=h).json()["order"]["note"]


def test_order_validation_and_store_scope(env):
    c, e = env; h = tok(c); loc, sku, have = stocked(e)
    P = lambda **k: c.post("/api/crm/orders", headers=h, json=dict(dict(phone="+91-9000011124", lines=[dict(sku_id=sku, qty=1)], fulfil_location_id=loc), **k))
    assert P(phone="12").status_code == 422
    assert P(lines=[]).status_code == 422 and P(source="fax").status_code == 422
    assert P(lines=[dict(sku_id=sku, qty=0)]).status_code == 422
    assert P(lines=[dict(sku_id=sku, qty=have + 1)]).status_code == 422 if have < 99 else True        # more than the store holds
    assert P(lines=[dict(sku_id=sku, qty=1), dict(sku_id=sku, qty=2)]).status_code == 422
    loose = q(e, "select id from sku where base_uom='g' and status='active' limit 1")[0][0]; assert P(lines=[dict(sku_id=loose, qty=1)]).status_code == 422
    assert P(fulfil_location_id=None).status_code == 422
    sm = tok(c, "storemgr"); assert c.post("/api/crm/orders", headers=sm, json=dict(phone="+91-9000011125", lines=[dict(sku_id=sku, qty=1)], fulfil_location_id=loc)).status_code == 403
    mine = c.get("/api/crm/orders?size=100", headers=sm).json(); assert mine["total"] > 0
    assert all(i["store"] == "Dry Fruit House HSR Layout" for i in mine["items"])
    foreign = q(e, "select id from wa_order where fulfil_location_id != 21 limit 1")[0][0]
    assert c.get(f"/api/crm/orders/{foreign}", headers=sm).status_code == 404
    assert c.post(f"/api/crm/orders/{foreign}/action", headers=sm, json=dict(action="confirm")).status_code == 404


def test_inbox_window_and_templates(env):
    c, e = env; h = tok(c)
    inbox = c.get("/api/crm/inbox", headers=h).json(); assert inbox["counts"]["unanswered"] >= 12 and inbox["counts"]["past_window"] >= 4
    past = [x for x in inbox["items"] if not x["window_open"]][0]; inside = [x for x in inbox["items"] if x["window_open"]][0]
    assert c.post(f"/api/crm/inbox/{past['customer_id']}/reply", headers=h, json=dict(body="Hello, sorry for the wait")).status_code == 422     # window closed
    assert c.post(f"/api/crm/inbox/{past['customer_id']}/reply", headers=h, json=dict(template="order_confirm")).status_code == 200            # approved utility template is fine
    assert c.post(f"/api/crm/inbox/{inside['customer_id']}/reply", headers=h, json=dict(body="Hello, here are the details")).status_code == 200
    assert c.post(f"/api/crm/inbox/{inside['customer_id']}/reply", headers=h, json=dict(body="  ")).status_code == 422
    assert c.post(f"/api/crm/inbox/{inside['customer_id']}/reply", headers=h, json=dict(template="not_a_template")).status_code == 422
    noc = q(e, "select id from customer where consent_wa=0 and id in (select customer_id from wa_message) limit 1")[0][0]
    assert c.post(f"/api/crm/inbox/{noc}/reply", headers=h, json=dict(template="new_arrival")).status_code == 422                            # marketing needs opt-in
    after = c.get("/api/crm/inbox", headers=h).json()["counts"]["unanswered"]; assert after == inbox["counts"]["unanswered"] - 2


def test_campaign_rules(env):
    c, e = env; mk = tok(c, "marketing"); ceo = tok(c)
    base = dict(name="Test send to new customers", template="new_arrival", segment="New")
    assert c.post("/api/crm/campaigns", headers=mk, json=dict(base, scheduled_at="2026-11-03T22:30")).status_code == 422                       # quiet hours
    assert c.post("/api/crm/campaigns", headers=mk, json=dict(base, scheduled_at="2026-10-01T10:00")).status_code == 422                       # in the past
    assert c.post("/api/crm/campaigns", headers=mk, json=dict(base, template="order_confirm")).status_code == 422                              # utility template
    assert c.post("/api/crm/campaigns", headers=mk, json=dict(base, segment="Everyone")).status_code == 422
    loyal = c.post("/api/crm/campaigns", headers=mk, json=dict(base, name="Loyal again", segment="Loyal")).json()
    assert loyal["suppressed"] > 0.8 * loyal["audience"]                                                                                         # messaged 3 days ago
    allc = c.post("/api/crm/campaigns", headers=mk, json=dict(base, name="Everyone", segment="All")).json()
    assert c.post(f"/api/crm/campaigns/{allc['id']}/action", headers=mk, json=dict(action="send")).status_code == 403                          # broad sends need CEO or admin
    n = c.post("/api/crm/campaigns", headers=mk, json=dict(base)).json(); assert n["eligible"] == n["audience"] - n["suppressed"] > 0
    consented = q(e, "select count(*) from customer where segment='New' and consent_wa=1")[0][0]; assert n["audience"] == consented
    r = c.post(f"/api/crm/campaigns/{n['id']}/action", headers=mk, json=dict(action="send")); assert r.status_code == 200 and r.json()["sent"] == n["eligible"]
    assert q(e, "select count(*) from customer where consent_wa=0 and last_marketing_at is not null")[0][0] == 0
    assert q(e, "select count(*) from customer where segment='New' and consent_wa=1 and last_marketing_at >= '2026-10-31'")[0][0] == r.json()["sent"]
    again = c.post("/api/crm/campaigns", headers=mk, json=dict(base)).json(); assert again["eligible"] == 0                                    # frequency cap now bites
    assert c.post(f"/api/crm/campaigns/{again['id']}/action", headers=mk, json=dict(action="send")).status_code == 422
    assert c.post(f"/api/crm/campaigns/{n['id']}/action", headers=mk, json=dict(action="cancel")).status_code == 422                          # already queued
    assert c.post(f"/api/crm/campaigns/{allc['id']}/action", headers=ceo, json=dict(action="cancel")).status_code == 200
    ok = c.post("/api/crm/campaigns", headers=mk, json=dict(base, name="Scheduled", scheduled_at="2026-11-03T10:00")); assert ok.status_code == 200


def test_quotes(env):
    c, e = env; h = tok(c)
    kit = q(e, "select id from sku where procurement_type='kit' and status='active' and mrp_paise is not null limit 1")[0][0]; mrp = q(e, "select mrp_paise from sku where id=:i", i=kit)[0][0]
    base = dict(company="Test Corp Pvt Ltd", contact="Asha Rao", city="Bengaluru", lines=[dict(sku_id=kit, qty=300)])
    assert c.post("/api/crm/quotes", headers=h, json=dict(base, lines=[dict(sku_id=kit, qty=5)])).status_code == 422
    plain = q(e, "select id from sku where procurement_type='finished' limit 1")[0][0]; assert c.post("/api/crm/quotes", headers=h, json=dict(base, lines=[dict(sku_id=plain, qty=50)])).status_code == 422
    assert c.post("/api/crm/quotes", headers=h, json=dict(base, valid_days=60)).status_code == 422
    assert c.post("/api/crm/quotes", headers=tok(c, "storemgr"), json=base).status_code == 403
    r = c.post("/api/crm/quotes", headers=h, json=base).json(); assert r["total_paise"] == 300 * int(mrp * 0.90)
    qid = r["id"]; d = c.get(f"/api/crm/quotes/{qid}", headers=h).json()
    assert d["lines"][0]["discount_pct"] == 10 and "can_build" in d["lines"][0] and d["quote"]["shown"] == "draft"
    A = lambda a, **k: c.post(f"/api/crm/quotes/{qid}/action", headers=h, json=dict(action=a, **k))
    assert A("accept").status_code == 422 and A("send").status_code == 200 and A("send").status_code == 422
    assert A("lost").status_code == 422
    nb = q(e, "select count(*) from bulk_order")[0][0]
    assert A("accept").status_code == 200 and q(e, "select count(*) from bulk_order")[0][0] == nb + 1
    assert q(e, "select count(*) from bulk_order_line where order_id=(select max(id) from bulk_order)")[0][0] == 1
    lst = c.get("/api/crm/quotes", headers=h).json(); assert lst["counts"].get("expired", 0) >= 4
    old = [x for x in lst["items"] if x["shown"] == "expired" and x["status"] == "sent"]
    if old: assert c.post(f"/api/crm/quotes/{old[0]['id']}/action", headers=h, json=dict(action="accept")).status_code == 422


def test_control_tower_exceptions(env):
    c, _ = env
    types = {x["type"] for x in c.get("/api/ct/exceptions", headers=tok(c)).json()}
    assert {"wa_payment", "wa_inbox", "quote_expiry", "campaign_optout"} <= types
    sm = {x["type"] for x in c.get("/api/ct/exceptions", headers=tok(c, "storemgr")).json()}
    assert "quote_expiry" not in sm and "campaign_optout" not in sm
