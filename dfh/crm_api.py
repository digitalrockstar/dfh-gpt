"""Module 8 API: customers and WhatsApp. Customer 360, consent, loyalty, WhatsApp orders into dispatch, inbox, campaigns and corporate gifting quotes.
The WhatsApp Business API and the payment gateway are not connected: messages are written to wa_message with status "sent" and payments are marked by hand.
Money is in paise. Loyalty rate, point value and delivery fee are stated assumptions."""
import re
from datetime import datetime, timedelta
from typing import List, Optional
from fastapi import Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from . import stockpost

OPS = {"ceo", "admin", "marketing", "store_manager"}      # customers, orders, inbox (store manager sees own store only)
MKT = {"ceo", "admin", "marketing"}                       # campaigns, quotes, loyalty adjustments
FULL_PHONE = {"ceo", "admin"}
BIG_ROLES = {"ceo", "admin"}
SEGMENTS = ["New", "Regular", "Loyal", "Lapsing", "One-time"]
EARN_RS_PER_POINT, POINT_VALUE_PAISE = 50, 50
FREE_DELIVERY_ABOVE, DELIVERY_FEE = 99900, 4900
ADJ_LIMIT, CAMPAIGN_APPROVAL, FREQ_CAP_DAYS = 500, 5000, 7
WINDOW_H, UNANSWERED_H = 24, 2
FLOW = {"new": ["confirm", "cancel"], "confirmed": ["send_link", "cancel"], "pay_pending": ["mark_paid", "send_link", "cancel"], "paid": ["pack", "cancel"],
        "packed": ["dispatch", "cancel"], "dispatched": ["deliver"], "delivered": [], "cancelled": []}
NEXT = {"confirm": "confirmed", "send_link": "pay_pending", "mark_paid": "paid", "pack": "packed", "dispatch": "dispatched", "deliver": "delivered", "cancel": "cancelled"}
SOURCES = {"whatsapp", "call", "website"}
PHONE = re.compile(r"^\+?[0-9\-]{8,16}$")
money = lambda p: (lambda r: f"Rs {r / 1e7:.2f} Cr" if r >= 1e7 else f"Rs {r / 1e5:.1f} L" if r >= 1e5 else f"Rs {r:,.0f}")(abs(p) / 100)


class ConsentIn(BaseModel):
    action: str
    source: str


class LoyaltyIn(BaseModel):
    points: int
    reason: str


class LineIn(BaseModel):
    sku_id: int
    qty: int


class OrderIn(BaseModel):
    phone: str
    name: Optional[str] = None
    lines: List[LineIn]
    source: str = "whatsapp"
    fulfil_location_id: Optional[int] = None
    note: Optional[str] = None


class ActionIn(BaseModel):
    action: str
    note: Optional[str] = None


class ReplyIn(BaseModel):
    body: Optional[str] = None
    template: Optional[str] = None


class CampaignIn(BaseModel):
    name: str
    template: str
    segment: str
    scheduled_at: Optional[str] = None


class QuoteIn(BaseModel):
    company: str
    contact: str
    phone: Optional[str] = None
    city: Optional[str] = None
    valid_days: int = 14
    lines: List[LineIn]
    note: Optional[str] = None


def disc_for(q):
    return 0.12 if q >= 600 else 0.10 if q >= 300 else 0.05 if q >= 100 else 0.0


def attach(app, ctx):
    me, need, many, one, audit, engine, NOW = (ctx[k] for k in ("me", "need", "many", "one", "audit", "engine", "NOW"))
    TODAY = NOW.date()
    ops, mkt = need(OPS), need(MKT)

    def store_of(u): return u["scope_id"] if u["scope_type"] == "store" else None

    def mask(p, u): return p if u["role"] in FULL_PHONE else (p[:4] + "••••" + p[-4:] if p and len(p) > 8 else p)

    def cust_or_404(cid, u):
        r = one("select * from customer where id=:i", i=cid)
        st = store_of(u)
        if not r or (st is not None and r["first_store_id"] != st): raise HTTPException(404, "Customer not found")
        return r

    def pager(page, size): return max(1, page), max(5, min(size, 100))

    # ---------------- options ----------------
    @app.get("/api/crm/options")
    def options(u=Depends(ops)):
        return dict(can_campaign=u["role"] in MKT, can_quotes=u["role"] in MKT, can_adjust=u["role"] in MKT, full_phone=u["role"] in FULL_PHONE, segments=SEGMENTS,
                    store_scope=store_of(u), asof=str(NOW), adj_limit=ADJ_LIMIT, campaign_approval=CAMPAIGN_APPROVAL, freq_cap_days=FREQ_CAP_DAYS,
                    assumptions=dict(earn=f"1 point per Rs {EARN_RS_PER_POINT}", point_value=f"1 point = Rs {POINT_VALUE_PAISE / 100:.2f}", delivery=f"Rs {DELIVERY_FEE / 100:.0f}, free above Rs {FREE_DELIVERY_ABOVE / 100:,.0f}",
                                     discounts="Quotes: 5% from 100 boxes, 10% from 300, 12% from 600"),
                    stores=many("select id, name, city from location where type='store' order by city, name") if store_of(u) is None else many("select id, name, city from location where id=:i", i=store_of(u)),
                    flow=FLOW, notice="WhatsApp Business API and the payment gateway are not connected. Messages are logged as sent and payments are marked by hand.")

    # ---------------- summary ----------------
    def unanswered(u, older_h=UNANSWERED_H):
        st = store_of(u); cw = " and c.first_store_id=:st" if st is not None else ""
        rows = many("select m.customer_id, m.ts, m.body from wa_message m join (select customer_id, max(ts) mt from wa_message group by customer_id) x on x.customer_id=m.customer_id and x.mt=m.ts "
                    "join customer c on c.id=m.customer_id where m.direction='in'" + cw, st=st)
        out = []
        for r in rows:
            ts = r["ts"] if isinstance(r["ts"], datetime) else datetime.fromisoformat(str(r["ts"]))
            h = (NOW - ts).total_seconds() / 3600
            if h >= older_h: out.append(dict(customer_id=r["customer_id"], hours=h, body=r["body"]))
        return out

    @app.get("/api/crm/summary")
    def summary(u=Depends(ops)):
        st = store_of(u); cw = " and first_store_id=:st" if st is not None else ""; ow = " and fulfil_location_id=:st" if st is not None else ""
        d30, d90 = str(TODAY - timedelta(days=30)), str(TODAY - timedelta(days=90))
        k = one("select count(*) n, coalesce(sum(case when consent_wa then 1 else 0 end),0) cons, coalesce(sum(case when last_order_on>=:d90 then 1 else 0 end),0) active90, coalesce(sum(case when created_at>=:d30 then 1 else 0 end),0) new30, "
                "coalesce(sum(case when orders_n>=2 then 1 else 0 end),0) rep, coalesce(sum(loyalty_points),0) pts, coalesce(sum(spend_paise),0) spend from customer where 1=1" + cw, d30=d30, d90=d90, st=st)
        segs = many("select segment, count(*) n, coalesce(sum(spend_paise),0) spend, avg(orders_n) avg_orders, coalesce(sum(case when consent_wa then 1 else 0 end),0) cons from customer where 1=1" + cw + " group by segment", st=st)
        order = {s: i for i, s in enumerate(SEGMENTS)}; segs.sort(key=lambda r: order.get(r["segment"], 9))
        w = many("select status, count(*) n, coalesce(sum(total_paise),0) v, min(created_at) oldest from wa_order where created_at>=:d" + ow + " group by status", d=str(NOW - timedelta(days=30)), st=st)
        wm = {r["status"]: r for r in w}
        live = [r for r in w if r["status"] != "cancelled"]
        aged = one("select count(*) n, coalesce(sum(total_paise),0) v from wa_order where status='pay_pending' and created_at<:t" + ow, t=str(NOW - timedelta(hours=24)), st=st)
        done = sum(r["n"] for r in w if r["status"] in ("delivered", "dispatched", "packed", "paid")); tot = sum(r["n"] for r in w if r["status"] != "new")
        ua = unanswered(u)
        res = dict(customers=k["n"], consented=k["cons"], consent_pct=(k["cons"] / k["n"] if k["n"] else 0), active90=k["active90"], new30=k["new30"], repeat_pct=(k["rep"] / k["n"] if k["n"] else 0),
                   points=k["pts"], liability=k["pts"] * POINT_VALUE_PAISE, spend=k["spend"], segments=segs,
                   wa=dict(orders=sum(r["n"] for r in live), value=sum(r["v"] for r in live), cancelled=wm.get("cancelled", dict(n=0))["n"], open=sum(wm.get(s, dict(n=0))["n"] for s in ("new", "confirmed", "pay_pending", "paid", "packed")),
                           unpaid_aged=aged["n"], unpaid_aged_value=aged["v"], paid_pct=(done / tot if tot else 0)),
                   inbox=dict(unanswered=len(ua), past_window=sum(1 for x in ua if x["hours"] >= WINDOW_H)))
        if u["role"] in MKT:
            q = one("select count(*) n, coalesce(sum(total_paise),0) v from quote where status='sent' and valid_to>=:t", t=str(TODAY))
            acc = one("select count(*) n, coalesce(sum(total_paise),0) v from quote where status='accepted' and created_at>=:d", d=str(NOW - timedelta(days=60)))
            lost = one("select count(*) n from quote where status='lost' and created_at>=:d", d=str(NOW - timedelta(days=60)))
            res["quotes"] = dict(open=q["n"], open_value=q["v"], won=acc["n"], won_value=acc["v"], win_rate=(acc["n"] / (acc["n"] + lost["n"]) if acc["n"] + lost["n"] else None))
            cs = many("select name, sent, optouts, scheduled_at from campaign where status in ('sent','queued') and sent>0 order by scheduled_at desc limit 5")
            res["campaigns"] = [dict(name=c["name"], sent=c["sent"], optout_pct=c["optouts"] / c["sent"]) for c in cs]
        return res

    # ---------------- customers ----------------
    SORTS = {"spend": "spend_paise desc", "recent": "last_order_on desc", "orders": "orders_n desc", "points": "loyalty_points desc", "new": "created_at desc"}

    @app.get("/api/crm/customers")
    def customers(q: str = "", segment: str = "", city: str = "", consent: str = "", sort: str = "spend", page: int = 1, size: int = 25, u=Depends(ops)):
        page, size = pager(page, size); st = store_of(u); w, p = " where 1=1", dict(st=st, lim=size, off=(page - 1) * size)
        if st is not None: w += " and first_store_id=:st"
        if q.strip():
            w += " and (lower(coalesce(name,'')) like :q or phone like :q or code like :q)"; p["q"] = f"%{q.strip().lower()}%"
        if segment in SEGMENTS: w += " and segment=:sg"; p["sg"] = segment
        if city: w += " and city=:ci"; p["ci"] = city
        if consent in ("0", "1"): w += " and consent_wa=:co"; p["co"] = int(consent)
        total = one("select count(*) n from customer" + w, **p)["n"]
        items = many("select id, code, phone, name, city, segment, orders_n, spend_paise, last_order_on, loyalty_points, consent_wa, created_at from customer" + w + f" order by {SORTS.get(sort, SORTS['spend'])}, id limit :lim offset :off", **p)
        for r in items: r["phone"] = mask(r["phone"], u)
        cities = [r["city"] for r in many("select distinct city from customer order by city")]
        return dict(total=total, page=page, size=size, items=items, cities=cities)

    @app.get("/api/crm/customers/{cid}")
    def customer(cid: int, u=Depends(ops)):
        c = cust_or_404(cid, u); c["phone"] = mask(c["phone"], u)
        last = c["last_order_on"]; rec = (TODAY - (last if hasattr(last, "year") else datetime.fromisoformat(str(last)).date())).days if last else None
        c["recency_days"] = rec
        pur = many("select p.business_date, p.net_paise, p.items, p.top_category, l.name store from customer_purchase p join location l on l.id=p.store_id where p.customer_id=:i order by p.business_date desc limit 15", i=cid)
        mon = many("select substr(business_date,1,7) m, count(*) n, sum(net_paise) v from customer_purchase where customer_id=:i group by 1 order by 1", i=cid)
        cats = many("select top_category c, count(*) n, sum(net_paise) v from customer_purchase where customer_id=:i group by 1 order by v desc limit 3", i=cid)
        loy = many("select ts, points, kind, note from loyalty_txn where customer_id=:i order by ts desc limit 12", i=cid)
        wo = many("select id, order_no, status, total_paise, created_at from wa_order where customer_id=:i order by created_at desc limit 8", i=cid)
        msgs = many("select ts, direction, kind, body, status from wa_message where customer_id=:i order by ts desc limit 8", i=cid)
        c["points_value"] = c["loyalty_points"] * POINT_VALUE_PAISE
        return dict(customer=c, purchases=pur, months=mon, categories=cats, loyalty=loy, orders=wo, messages=msgs, can_adjust=u["role"] in MKT)

    @app.post("/api/crm/customers/{cid}/consent")
    def consent(cid: int, b: ConsentIn, u=Depends(ops)):
        c = cust_or_404(cid, u)
        if b.action not in ("grant", "revoke"): raise HTTPException(422, "Action must be grant or revoke")
        ok = {"grant": {"counter", "whatsapp_optin", "website", "phone_call"}, "revoke": {"stop_reply", "request", "complaint"}}[b.action]
        if b.source not in ok: raise HTTPException(422, "Source must be one of: " + ", ".join(sorted(ok)))
        new = 1 if b.action == "grant" else 0
        if bool(c["consent_wa"]) == bool(new): raise HTTPException(422, "Already " + ("opted in" if new else "opted out"))
        with engine.begin() as cn:
            cn.execute(text("update customer set consent_wa=:v, consent_at=:t where id=:i"), dict(v=new, t=NOW, i=cid))
            audit(cn, u, "crm.consent." + b.action, "customer", cid, dict(consent=bool(c["consent_wa"])), dict(consent=bool(new), source=b.source))
        return dict(ok=True, consent_wa=bool(new))

    @app.post("/api/crm/customers/{cid}/loyalty")
    def loyalty(cid: int, b: LoyaltyIn, u=Depends(mkt)):
        c = cust_or_404(cid, u)
        if b.points == 0 or abs(b.points) > 2000: raise HTTPException(422, "Points must be between 1 and 2,000, up or down")
        if len(b.reason.strip()) < 5: raise HTTPException(422, "Give a reason of at least 5 characters")
        if abs(b.points) > ADJ_LIMIT and u["role"] not in BIG_ROLES: raise HTTPException(403, f"Adjustments above {ADJ_LIMIT} points need the CEO or admin")
        if c["loyalty_points"] + b.points < 0: raise HTTPException(422, f"The balance is {c['loyalty_points']} points and cannot go below zero")
        with engine.begin() as cn:
            cn.execute(text("insert into loyalty_txn(customer_id, sale_id, points, ts, kind, note) values (:c,null,:p,:t,'adjust',:n)"), dict(c=cid, p=b.points, t=NOW, n=b.reason.strip()[:80]))
            cn.execute(text("update customer set loyalty_points=loyalty_points+:p where id=:i"), dict(p=b.points, i=cid))
            audit(cn, u, "crm.loyalty.adjust", "customer", cid, dict(points=c["loyalty_points"]), dict(points=c["loyalty_points"] + b.points, reason=b.reason.strip()))
        return dict(ok=True, points=c["loyalty_points"] + b.points)

    # ---------------- WhatsApp orders ----------------
    def avail(cn, loc, sid):
        return cn.execute(text("select coalesce(sum(b.qty),0) from stock_balance b join batch t on t.id=b.batch_id where b.location_id=:l and b.sku_id=:s and t.expiry_date>=:d"), dict(l=loc, s=sid, d=str(TODAY))).scalar()

    def say(cn, cid, kind, body, template=None, order_id=None):
        cn.execute(text("insert into wa_message(customer_id, direction, ts, kind, body, template, status, order_id, campaign_id) values (:c,'out',:t,:k,:b,:tp,'sent',:o,null)"),
                   dict(c=cid, t=NOW, k=kind, b=body[:1000], tp=template, o=order_id))

    def render(cn, code, cust, order=None):
        t = cn.execute(text("select body from wa_template where code=:c and approved=true"), dict(c=code)).scalar()
        if not t: raise HTTPException(422, "That template is not approved")
        return t.format(name=cust["name"] or "there", order_no=(order or {}).get("order_no", ""), total=f"{(order or {}).get('total_paise', 0) / 100:,.0f}", link=(order or {}).get("payment_link", ""))

    @app.get("/api/crm/orders")
    def orders(status: str = "", q: str = "", aged: int = 0, page: int = 1, size: int = 25, u=Depends(ops)):
        page, size = pager(page, size); st = store_of(u); w, p = " where 1=1", dict(st=st, lim=size, off=(page - 1) * size)
        if st is not None: w += " and o.fulfil_location_id=:st"
        if status in FLOW: w += " and o.status=:s"; p["s"] = status
        elif status == "open": w += " and o.status in ('new','confirmed','pay_pending','paid','packed')"
        if q.strip(): w += " and (o.order_no like :q or lower(coalesce(c.name,'')) like :q or c.phone like :q)"; p["q"] = f"%{q.strip().lower()}%"
        if aged: w += " and o.status='pay_pending' and o.created_at<:t"; p["t"] = str(NOW - timedelta(hours=24))
        base = " from wa_order o join customer c on c.id=o.customer_id join location l on l.id=o.fulfil_location_id" + w
        total = one("select count(*) n" + base, **p)["n"]
        items = many("select o.id, o.order_no, o.status, o.source, o.created_at, o.total_paise, o.city, c.id cid, c.name, c.phone, l.name store" + base + " order by o.created_at desc limit :lim offset :off", **p)
        for r in items:
            ts = r["created_at"] if isinstance(r["created_at"], datetime) else datetime.fromisoformat(str(r["created_at"]))
            r["age_h"] = int((NOW - ts).total_seconds() / 3600); r["phone"] = mask(r["phone"], u); r["stuck"] = r["status"] == "pay_pending" and r["age_h"] >= 24
        counts = {r["status"]: r["n"] for r in many("select status, count(*) n from wa_order o" + (" where fulfil_location_id=:st" if st is not None else "") + " group by status", st=st)}
        return dict(total=total, page=page, size=size, items=items, counts=counts)

    def order_or_404(oid, u):
        o = one("select o.*, l.name store, l.city store_city from wa_order o join location l on l.id=o.fulfil_location_id where o.id=:i", i=oid)
        st = store_of(u)
        if not o or (st is not None and o["fulfil_location_id"] != st): raise HTTPException(404, "Order not found")
        return o

    @app.get("/api/crm/orders/{oid}")
    def order(oid: int, u=Depends(ops)):
        o = order_or_404(oid, u)
        c = one("select id, code, name, phone, city, consent_wa from customer where id=:i", i=o["customer_id"]); c["phone"] = mask(c["phone"], u)
        ls = many("select l.sku_id, s.name, s.code, l.qty, l.price_paise from wa_order_line l join sku s on s.id=l.sku_id where l.order_id=:i", i=oid)
        ms = many("select ts, direction, kind, body, template, status from wa_message where order_id=:i order by ts", i=oid)
        with engine.connect() as cn:
            for l in ls: l["available"] = avail(cn, o["fulfil_location_id"], l["sku_id"])
        return dict(order=o, customer=c, lines=ls, messages=ms, actions=FLOW[o["status"]], subtotal=sum(l["qty"] * l["price_paise"] for l in ls))

    @app.post("/api/crm/orders")
    def order_create(b: OrderIn, u=Depends(ops)):
        phone = b.phone.strip()
        if not PHONE.match(phone): raise HTTPException(422, "Phone looks wrong. Use digits with an optional + and dashes")
        if b.source not in SOURCES: raise HTTPException(422, "Source must be whatsapp, call or website")
        if not b.lines or len(b.lines) > 20: raise HTTPException(422, "An order needs 1 to 20 lines")
        if len({l.sku_id for l in b.lines}) != len(b.lines): raise HTTPException(422, "Each SKU can appear once")
        if any(l.qty < 1 or l.qty > 99 for l in b.lines): raise HTTPException(422, "Quantity must be 1 to 99 per line")
        st = store_of(u)
        with engine.begin() as cn:
            cu = cn.execute(text("select * from customer where phone=:p"), dict(p=phone)).mappings().fetchone()
            if cu and st is not None and cu["first_store_id"] != st: raise HTTPException(403, "That customer belongs to another store")
            loc = b.fulfil_location_id or st or (cu["first_store_id"] if cu else None)
            if not loc: raise HTTPException(422, "Pick the store that will fulfil this order")
            if st is not None and loc != st: raise HTTPException(403, "A store manager can only create orders for their own store")
            sl = cn.execute(text("select id, city from location where id=:i and type='store'"), dict(i=loc)).mappings().fetchone()
            if not sl: raise HTTPException(422, "Fulfilling location must be a store")
            sub, rows = 0, []
            for l in b.lines:
                s = cn.execute(text("select id, name, mrp_paise, base_uom, status, procurement_type from sku where id=:i"), dict(i=l.sku_id)).mappings().fetchone()
                if not s or s["status"] != "active": raise HTTPException(422, f"SKU {l.sku_id} is not active")
                if s["base_uom"] != "ea" or s["mrp_paise"] is None: raise HTTPException(422, f"{s['name']} is sold loose and cannot go on a WhatsApp order")
                have = avail(cn, loc, s["id"])
                if have < l.qty: raise HTTPException(422, f"Only {have} of {s['name']} left at the chosen store. Pick fewer or another store")
                rows.append((s["id"], l.qty, s["mrp_paise"])); sub += l.qty * s["mrp_paise"]
            if cu is None:
                nid = cn.execute(text("select coalesce(max(id),0)+1 from customer")).scalar()
                cn.execute(text("insert into customer(id, code, phone, name, city, created_at, first_store_id, consent_wa, loyalty_points, segment, orders_n, spend_paise) values (:i,:c,:p,:n,:ci,:t,:f,0,0,'New',0,0)"),
                           dict(i=nid, c=f"C{nid:06d}", p=phone, n=(b.name or "").strip() or None, ci=sl["city"], t=NOW, f=loc))
                cid = nid
            else: cid = cu["id"]
            fee = 0 if sub >= FREE_DELIVERY_ABOVE else DELIVERY_FEE
            seq = cn.execute(text("select coalesce(max(id),0)+1 from wa_order")).scalar()
            no = f"WA-{NOW:%y%m}-{seq:05d}"
            cn.execute(text("insert into wa_order(order_no, customer_id, status, source, created_at, total_paise, fulfil_location_id, updated_at, city, note, delivery_paise) values (:n,:c,'new',:s,:t,:tot,:l,:t,:ci,:no,:d)"),
                       dict(n=no, c=cid, s=b.source, t=NOW, tot=sub + fee, l=loc, ci=sl["city"], no=(b.note or "")[:200] or None, d=fee))
            oid = cn.execute(text("select id from wa_order where order_no=:n"), dict(n=no)).scalar()
            for sid, q, p in rows: cn.execute(text("insert into wa_order_line(order_id, sku_id, qty, price_paise) values (:o,:s,:q,:p)"), dict(o=oid, s=sid, q=q, p=p))
            audit(cn, u, "crm.order.create", "wa_order", oid, None, dict(order_no=no, total=sub + fee, lines=len(rows)))
        return dict(id=oid, order_no=no, total_paise=sub + fee, delivery_paise=fee)

    @app.post("/api/crm/orders/{oid}/action")
    def order_action(oid: int, b: ActionIn, u=Depends(ops)):
        o = order_or_404(oid, u)
        if b.action not in FLOW[o["status"]]: raise HTTPException(422, f"An order that is {o['status'].replace('_', ' ')} cannot {b.action.replace('_', ' ')}")
        note = (b.note or "").strip()
        if b.action == "cancel" and len(note) < 5: raise HTTPException(422, "Give a reason for cancelling")
        new = NEXT[b.action]
        with engine.begin() as cn:
            cu = dict(cn.execute(text("select * from customer where id=:i"), dict(i=o["customer_id"])).mappings().fetchone())
            ls = cn.execute(text("select l.sku_id, l.qty, s.name from wa_order_line l join sku s on s.id=l.sku_id where l.order_id=:i"), dict(i=oid)).mappings().fetchall()
            upd = dict(s=new, t=NOW, i=oid); extra = ""
            if b.action in ("confirm", "send_link"):
                for l in ls:
                    have = avail(cn, o["fulfil_location_id"], l["sku_id"])
                    if have < l["qty"] and o["status"] == "new": raise HTTPException(422, f"Only {have} of {l['name']} left at {o['store']}. Edit the order or cancel it")
            if b.action == "confirm":
                say(cn, cu["id"], "template", render(cn, "order_confirm", cu, o), "order_confirm", oid)
            elif b.action == "send_link":
                link = o["payment_link"] or f"https://pay.dfh-demo.example/p/{o['order_no']}"
                o2 = dict(o, payment_link=link); extra = ", payment_link=:pl"; upd["pl"] = link
                say(cn, cu["id"], "template", render(cn, "payment_link", cu, o2), "payment_link", oid)
            elif b.action == "mark_paid": extra = ", paid_at=:t"
            elif b.action == "dispatch":
                for l in ls:
                    used, short = stockpost.take_fefo(cn, NOW, o["fulfil_location_id"], l["sku_id"], l["qty"], TODAY, "wa_order", o["order_no"], u["id"])
                    if short > 0: raise HTTPException(422, f"{o['store']} is short by {short} of {l['name']}. Nothing was dispatched")
                say(cn, cu["id"], "template", render(cn, "dispatch_update", cu, o), "dispatch_update", oid)
            elif b.action == "deliver": extra = ", delivered_at=:t"
            elif b.action == "cancel":
                extra = ", cancel_reason=:cr, note=:nt"; upd["cr"] = note[:80]
                upd["nt"] = (f"Refund of {money(o['total_paise'])} due. " if o["status"] in ("paid", "packed") else "") + (o["note"] or "")
            cn.execute(text(f"update wa_order set status=:s, updated_at=:t{extra} where id=:i"), upd)
            audit(cn, u, "crm.order." + b.action, "wa_order", oid, dict(status=o["status"]), dict(status=new, note=note or None))
        return dict(ok=True, status=new, refund_due=(b.action == "cancel" and o["status"] in ("paid", "packed")))

    # ---------------- inbox ----------------
    def convs(u):
        st = store_of(u); cw = " where c.first_store_id=:st" if st is not None else ""
        rows = many("select m.customer_id cid, max(m.ts) last_ts, max(case when m.direction='in' then m.ts end) last_in, count(*) n from wa_message m join customer c on c.id=m.customer_id" + cw + " group by m.customer_id", st=st)
        return rows

    def dt(x): return x if isinstance(x, datetime) else (datetime.fromisoformat(str(x)) if x else None)

    @app.get("/api/crm/inbox")
    def inbox(filter: str = "unanswered", page: int = 1, size: int = 25, u=Depends(ops)):
        page, size = pager(page, size); out = []
        for r in convs(u):
            lt, li = dt(r["last_ts"]), dt(r["last_in"])
            last = one("select direction, body from wa_message where customer_id=:c order by ts desc, id desc limit 1", c=r["cid"])
            wait = last["direction"] == "in"
            hrs = (NOW - lt).total_seconds() / 3600
            if filter == "unanswered" and not (wait and hrs >= UNANSWERED_H): continue
            if filter == "window" and not (li and (NOW - li).total_seconds() / 3600 < WINDOW_H): continue
            out.append(dict(customer_id=r["cid"], last_ts=str(lt), hours=round(hrs, 1), waiting=wait, window_open=bool(li and (NOW - li).total_seconds() / 3600 < WINDOW_H), body=last["body"][:140]))
        out.sort(key=lambda x: (not x["waiting"], x["hours"] if not x["waiting"] else -x["hours"]))
        total = len(out); out = out[(page - 1) * size: page * size]
        if out:
            ids = ",".join(str(x["customer_id"]) for x in out)
            cm = {c["id"]: c for c in many(f"select id, name, phone, city, consent_wa from customer where id in ({ids})")}
            for x in out:
                c = cm[x["customer_id"]]; x.update(name=c["name"], phone=mask(c["phone"], u), city=c["city"], consent_wa=bool(c["consent_wa"]))
        un = unanswered(u)
        return dict(total=total, page=page, size=size, items=out, counts=dict(unanswered=len(un), past_window=sum(1 for x in un if x["hours"] >= WINDOW_H)))

    @app.get("/api/crm/inbox/{cid}")
    def thread(cid: int, u=Depends(ops)):
        c = cust_or_404(cid, u); c["phone"] = mask(c["phone"], u)
        ms = many("select id, ts, direction, kind, body, template, status, order_id from wa_message where customer_id=:c order by ts, id", c=cid)
        ins = [dt(m["ts"]) for m in ms if m["direction"] == "in"]
        wo = bool(ins and (NOW - max(ins)).total_seconds() / 3600 < WINDOW_H)
        tp = many("select code, name, category, body from wa_template where approved=true order by category, name")
        return dict(customer=dict(id=c["id"], name=c["name"], phone=c["phone"], city=c["city"], consent_wa=bool(c["consent_wa"]), segment=c["segment"]), messages=ms[-60:], window_open=wo, templates=tp)

    @app.post("/api/crm/inbox/{cid}/reply")
    def reply(cid: int, b: ReplyIn, u=Depends(ops)):
        c = cust_or_404(cid, u)
        ins = many("select ts from wa_message where customer_id=:c and direction='in' order by ts desc limit 1", c=cid)
        wo = bool(ins and (NOW - dt(ins[0]["ts"])).total_seconds() / 3600 < WINDOW_H)
        with engine.begin() as cn:
            if b.template:
                t = cn.execute(text("select code, category, approved from wa_template where code=:c"), dict(c=b.template)).mappings().fetchone()
                if not t or not t["approved"]: raise HTTPException(422, "Pick an approved template")
                if t["category"] == "marketing" and not c["consent_wa"]: raise HTTPException(422, "This customer has not opted in to marketing messages")
                body = render(cn, b.template, c); say(cn, cid, "template", body, b.template)
            else:
                body = (b.body or "").strip()
                if not body or len(body) > 1000: raise HTTPException(422, "Write a message of 1 to 1,000 characters")
                if not wo: raise HTTPException(422, "The 24 hour window is closed. Send an approved template instead")
                say(cn, cid, "text", body)
            audit(cn, u, "crm.inbox.reply", "customer", cid, None, dict(kind="template" if b.template else "text"))
        return dict(ok=True)

    @app.get("/api/crm/templates")
    def templates(u=Depends(ops)):
        return many("select code, name, category, body, approved from wa_template order by category, name")

    # ---------------- campaigns ----------------
    def audience(segment):
        w = " where consent_wa=true" + ("" if segment == "All" else " and segment=:s")
        cut = str(NOW - timedelta(days=FREQ_CAP_DAYS))
        tot = one("select count(*) n from customer" + w, s=segment)["n"]
        sup = one("select count(*) n from customer" + w + " and last_marketing_at>=:c", s=segment, c=cut)["n"]
        return tot, sup

    @app.get("/api/crm/campaigns")
    def campaigns(u=Depends(mkt)):
        rows = many("select id, name, template, segment, scheduled_at, status, sent, delivered, replied, audience, suppressed, optouts from campaign order by coalesce(scheduled_at, created_at) desc")
        for r in rows:
            if r["status"] in ("draft", "scheduled"):
                a, s = audience(r["segment"]); r["audience"], r["suppressed"] = a, s
            r["eligible"] = max(r["audience"] - r["suppressed"], 0) if r["status"] in ("draft", "scheduled") else r["sent"]
            r["delivery_pct"] = r["delivered"] / r["sent"] if r["sent"] else None; r["reply_pct"] = r["replied"] / r["sent"] if r["sent"] else None; r["optout_pct"] = r["optouts"] / r["sent"] if r["sent"] else None
            r["needs_approval"] = r["segment"] == "All" or r["eligible"] > CAMPAIGN_APPROVAL
        med = sorted(r["optout_pct"] for r in rows if r["optout_pct"] is not None)
        return dict(items=rows, median_optout=(med[len(med) // 2] if med else None), templates=many("select code, name, body from wa_template where approved=true and category='marketing' order by name"), segments=SEGMENTS + ["All"])

    @app.post("/api/crm/campaigns")
    def campaign_create(b: CampaignIn, u=Depends(mkt)):
        name = b.name.strip()
        if not 3 <= len(name) <= 120: raise HTTPException(422, "Name must be 3 to 120 characters")
        if b.segment not in SEGMENTS + ["All"]: raise HTTPException(422, "Unknown segment")
        t = one("select category, approved from wa_template where code=:c", c=b.template)
        if not t or not t["approved"] or t["category"] != "marketing": raise HTTPException(422, "Pick an approved marketing template")
        at = None
        if b.scheduled_at:
            try: at = datetime.fromisoformat(b.scheduled_at)
            except ValueError: raise HTTPException(422, "Schedule time must look like 2026-11-03T10:00")
            if at <= NOW: raise HTTPException(422, "Schedule a time in the future")
            if not 9 <= at.hour < 21: raise HTTPException(422, "Quiet hours: marketing messages go out between 9 am and 9 pm")
        a, s = audience(b.segment)
        with engine.begin() as cn:
            cn.execute(text("insert into campaign(name, template, segment, scheduled_at, status, sent, delivered, replied, audience, suppressed, optouts, created_by, created_at) values (:n,:t,:s,:a,:st,0,0,0,:au,:su,0,:u,:c)"),
                       dict(n=name, t=b.template, s=b.segment, a=at, st="scheduled" if at else "draft", au=a, su=s, u=u["id"], c=NOW))
            cid = cn.execute(text("select max(id) from campaign")).scalar()
            audit(cn, u, "crm.campaign.create", "campaign", cid, None, dict(name=name, segment=b.segment, audience=a, suppressed=s))
        return dict(id=cid, audience=a, suppressed=s, eligible=a - s)

    @app.post("/api/crm/campaigns/{cid}/action")
    def campaign_action(cid: int, b: ActionIn, u=Depends(mkt)):
        c = one("select * from campaign where id=:i", i=cid)
        if not c: raise HTTPException(404, "Campaign not found")
        if c["status"] not in ("draft", "scheduled"): raise HTTPException(422, "Only a draft or scheduled campaign can be changed")
        if b.action == "cancel":
            with engine.begin() as cn:
                cn.execute(text("update campaign set status='cancelled' where id=:i"), dict(i=cid)); audit(cn, u, "crm.campaign.cancel", "campaign", cid, dict(status=c["status"]), dict(status="cancelled"))
            return dict(ok=True, status="cancelled")
        if b.action != "send": raise HTTPException(422, "Action must be send or cancel")
        a, s = audience(c["segment"]); elig = a - s
        if (c["segment"] == "All" or elig > CAMPAIGN_APPROVAL) and u["role"] not in BIG_ROLES:
            raise HTTPException(403, "Broad sends and audiences above " + f"{CAMPAIGN_APPROVAL:,}" + " need the CEO or admin")
        if elig <= 0: raise HTTPException(422, "Nobody is eligible. Everyone in this segment either has not opted in or was messaged in the last 7 days")
        w = " where consent_wa=true and (last_marketing_at is null or last_marketing_at<:cut)" + ("" if c["segment"] == "All" else " and segment=:s")
        with engine.begin() as cn:
            cn.execute(text("update customer set last_marketing_at=:t" + w), dict(t=NOW, cut=str(NOW - timedelta(days=FREQ_CAP_DAYS)), s=c["segment"]))
            cn.execute(text("update campaign set status='queued', sent=:e, audience=:a, suppressed=:s, approved_by=:u, scheduled_at=coalesce(scheduled_at,:t) where id=:i"), dict(e=elig, a=a, s=s, u=u["id"], t=NOW, i=cid))
            audit(cn, u, "crm.campaign.send", "campaign", cid, dict(status=c["status"]), dict(status="queued", sent=elig, suppressed=s))
        return dict(ok=True, status="queued", sent=elig, suppressed=s)

    # ---------------- quotes ----------------
    def qstatus(r):
        d = r["valid_to"] if hasattr(r["valid_to"], "year") else datetime.fromisoformat(str(r["valid_to"])).date()
        return ("expired" if r["status"] == "sent" and d < TODAY else r["status"]), (d - TODAY).days

    @app.get("/api/crm/quotes")
    def quotes(status: str = "", u=Depends(mkt)):
        rows = many("select q.id, q.quote_no, q.company, q.contact, q.status, q.created_at, q.valid_to, q.total_paise, q.city, (select count(*) from quote_line l where l.quote_id=q.id) n, (select coalesce(sum(qty),0) from quote_line l where l.quote_id=q.id) boxes from quote q order by q.created_at desc")
        for r in rows: r["shown"], r["days_left"] = qstatus(r); r["expiring"] = r["shown"] == "sent" and r["days_left"] <= 3
        counts = {}
        for r in rows: counts[r["shown"]] = counts.get(r["shown"], 0) + 1
        if status: rows = [r for r in rows if r["shown"] == status]
        return dict(items=rows, counts=counts)

    def kit_build(kit, need):
        comps = many("select b.component_sku_id sid, b.qty per, s.name from kit_bom b join sku s on s.id=b.component_sku_id where b.kit_sku_id=:k", k=kit)
        worst = None
        for c in comps:
            have = one("select coalesce(sum(b.qty),0) n from stock_balance b join batch t on t.id=b.batch_id join location l on l.id=b.location_id where l.type='warehouse' and b.sku_id=:s and t.expiry_date>=:d", s=c["sid"], d=str(TODAY))["n"]
            can = have // c["per"] if c["per"] else 0
            if worst is None or can < worst["can"]: worst = dict(can=int(can), limiter=c["name"])
        return worst or dict(can=0, limiter=None)

    @app.get("/api/crm/quotes/{qid}")
    def quote(qid: int, u=Depends(mkt)):
        q = one("select * from quote where id=:i", i=qid)
        if not q: raise HTTPException(404, "Quote not found")
        q["shown"], q["days_left"] = qstatus(q)
        ls = many("select l.sku_id, s.name, s.mrp_paise, l.qty, l.price_paise from quote_line l join sku s on s.id=l.sku_id where l.quote_id=:i", i=qid)
        for l in ls:
            l["discount_pct"] = round((1 - l["price_paise"] / l["mrp_paise"]) * 100) if l["mrp_paise"] else 0; l["line_total"] = l["qty"] * l["price_paise"]
            b = kit_build(l["sku_id"], l["qty"]); l["can_build"], l["limiter"] = b["can"], b["limiter"]; l["short"] = b["can"] < l["qty"]
        return dict(quote=q, lines=ls)

    @app.post("/api/crm/quotes")
    def quote_create(b: QuoteIn, u=Depends(mkt)):
        if not 3 <= len(b.company.strip()) <= 120 or not b.contact.strip(): raise HTTPException(422, "Company and contact are needed")
        if not 1 <= b.valid_days <= 30: raise HTTPException(422, "Validity must be 1 to 30 days")
        if not b.lines or len(b.lines) > 10: raise HTTPException(422, "A quote needs 1 to 10 gift box lines")
        if len({l.sku_id for l in b.lines}) != len(b.lines): raise HTTPException(422, "Each box can appear once")
        if b.phone and not PHONE.match(b.phone.strip()): raise HTTPException(422, "Phone looks wrong")
        rows, tot = [], 0
        for l in b.lines:
            s = one("select id, name, mrp_paise, procurement_type, status from sku where id=:i", i=l.sku_id)
            if not s or s["procurement_type"] != "kit" or s["status"] != "active" or not s["mrp_paise"]: raise HTTPException(422, f"SKU {l.sku_id} is not an active gift box")
            if not 10 <= l.qty <= 5000: raise HTTPException(422, f"{s['name']}: quantity must be 10 to 5,000")
            p = int(s["mrp_paise"] * (1 - disc_for(l.qty))); rows.append((s["id"], l.qty, p)); tot += l.qty * p
        with engine.begin() as cn:
            seq = cn.execute(text("select coalesce(max(id),0)+1 from quote")).scalar(); no = f"Q-{NOW:%y%m}-{seq:04d}"
            cn.execute(text("insert into quote(quote_no, company, contact, status, created_at, valid_to, total_paise, owner_id, city, phone, note) values (:n,:co,:ct,'draft',:t,:v,:tot,:o,:ci,:ph,:no)"),
                       dict(n=no, co=b.company.strip(), ct=b.contact.strip(), t=NOW, v=str(TODAY + timedelta(days=b.valid_days)), tot=tot, o=u["id"], ci=b.city, ph=(b.phone or "").strip() or None, no=(b.note or "")[:200] or None))
            qid = cn.execute(text("select id from quote where quote_no=:n"), dict(n=no)).scalar()
            for sid, q, p in rows: cn.execute(text("insert into quote_line(quote_id, sku_id, qty, price_paise) values (:q,:s,:n,:p)"), dict(q=qid, s=sid, n=q, p=p))
            audit(cn, u, "crm.quote.create", "quote", qid, None, dict(quote_no=no, total=tot))
        return dict(id=qid, quote_no=no, total_paise=tot)

    @app.post("/api/crm/quotes/{qid}/action")
    def quote_action(qid: int, b: ActionIn, u=Depends(mkt)):
        q = one("select * from quote where id=:i", i=qid)
        if not q: raise HTTPException(404, "Quote not found")
        shown, left = qstatus(q); note = (b.note or "").strip()
        allowed = {"draft": ["send"], "sent": ["accept", "lost"], "expired": [], "accepted": [], "lost": []}[shown]
        if b.action not in allowed: raise HTTPException(422, f"A {shown} quote cannot {b.action}" if shown != "expired" else "This quote has expired. Create a new one")
        if b.action == "lost" and len(note) < 5: raise HTTPException(422, "Give the reason it was lost")
        with engine.begin() as cn:
            if b.action == "send": cn.execute(text("update quote set status='sent' where id=:i"), dict(i=qid))
            elif b.action == "lost": cn.execute(text("update quote set status='lost', decided_at=:t, lost_reason=:r where id=:i"), dict(t=NOW, r=note[:80], i=qid))
            else:
                wh = cn.execute(text("select id from location where type='warehouse' and city=:c order by id limit 1"), dict(c=q["city"])).scalar() or cn.execute(text("select min(id) from location where type='warehouse'")).scalar()
                nb = cn.execute(text("select coalesce(max(id),0)+1 from bulk_order")).scalar()
                cn.execute(text("insert into bulk_order(order_no, customer_id, type, status, location_id, created_at, due_date, total_paise) values (:n,null,'corporate','new',:l,:t,:d,:tot)"),
                           dict(n=f"BO-{NOW:%y%m}-{nb:04d}", l=wh, t=NOW, d=str(TODAY + timedelta(days=14)), tot=q["total_paise"]))
                bid = cn.execute(text("select id from bulk_order where order_no=:n"), dict(n=f"BO-{NOW:%y%m}-{nb:04d}")).scalar()
                for l in cn.execute(text("select sku_id, qty, price_paise from quote_line where quote_id=:i"), dict(i=qid)).mappings().fetchall():
                    cn.execute(text("insert into bulk_order_line(order_id, sku_id, qty, price_paise) values (:o,:s,:q,:p)"), dict(o=bid, s=l["sku_id"], q=l["qty"], p=l["price_paise"]))
                cn.execute(text("update quote set status='accepted', decided_at=:t where id=:i"), dict(t=NOW, i=qid))
            audit(cn, u, "crm.quote." + b.action, "quote", qid, dict(status=q["status"]), dict(note=note or None))
        return dict(ok=True)

    # ---------------- Control Tower hooks ----------------
    def exceptions(f):
        out, sc = [], f["store_ids"]
        ow = "" if sc is None else " and fulfil_location_id in (" + ",".join(str(int(x)) for x in sc) + ")"
        a = one("select count(*) n, coalesce(sum(total_paise),0) v from wa_order where status='pay_pending' and created_at<:t" + ow, t=str(NOW - timedelta(hours=24)))
        if a["n"]:
            out.append(dict(type="wa_payment", severity="med", title=f"{a['n']} WhatsApp orders unpaid after 24 hours", detail=f"{money(a['v'])} waiting on a payment link. Resend the link or cancel so stock is not held.", ref="orders"))
        cw = "" if sc is None else " and first_store_id in (" + ",".join(str(int(x)) for x in sc) + ")"
        rows = many("select m.ts from wa_message m join (select customer_id, max(ts) mt from wa_message group by customer_id) x on x.customer_id=m.customer_id and x.mt=m.ts join customer c on c.id=m.customer_id where m.direction='in'" + cw)
        hrs = [(NOW - dt(r["ts"])).total_seconds() / 3600 for r in rows]; open_ = [h for h in hrs if h >= UNANSWERED_H]
        if open_:
            late = sum(1 for h in open_ if h >= WINDOW_H)
            out.append(dict(type="wa_inbox", severity="high" if late else "med", title=f"{len(open_)} customer messages waiting for a reply", detail=f"{late} are past the 24 hour window and now need an approved template." if late else "All are still inside the 24 hour window.", ref="inbox"))
        if sc is None:
            qs = many("select quote_no, company, total_paise, valid_to from quote where status='sent' and valid_to>=:t and valid_to<=:u", t=str(TODAY), u=str(TODAY + timedelta(days=3)))
            if qs:
                v = sum(x["total_paise"] for x in qs)
                out.append(dict(type="quote_expiry", severity="high" if v >= 30_000_000 else "med", title=f"{len(qs)} corporate gifting quotes expire within 3 days", detail=f"{money(v)} at stake. Largest: {max(qs, key=lambda x: x['total_paise'])['company']}.", ref="quotes"))
            for c in many("select name, sent, optouts from campaign where status in ('sent','queued') and sent>=1000 and optouts*1.0/sent>=0.02"):
                out.append(dict(type="campaign_optout", severity="med", title=f"Campaign \"{c['name']}\" lost {c['optouts'] / c['sent'] * 100:.1f}% of recipients to opt-outs", detail="That is several times the usual rate. Narrow the segment before the next send.", ref="campaigns"))
        return out

    return dict(exceptions=exceptions)
