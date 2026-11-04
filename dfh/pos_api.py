"""Module POS API: offline-first store billing. Devices bill against a reserved block of invoice numbers, queue bills locally and push them
with idempotency keys. Server recomputes every bill and decides: accepted, review, duplicate or rejected."""
import json, uuid
from datetime import datetime, date, timedelta
from typing import Optional, List
from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text
from .pos_calc import line_calc, bill_calc
from . import stockpost

BILL = {"cashier", "store_manager", "ceo", "admin"}
MANAGE = {"store_manager", "ceo", "admin"}
HEALTH = {"ceo", "finance", "admin", "store_manager", "procurement"}
MODES = {"cash", "upi", "card"}
RETURN_REASONS = {"defective", "damaged"}
BLOCK_MAX, UNUSED_CAP = 200, 500
LOW_PRICE, BIG_DISCOUNT = 0.95, 0.10        # review flags
VARIANCE_NOTE = 10000                       # paise, a cash variance above Rs 100 needs a note
FRESH_MIN, LATE_H = 60, 24                  # sync health thresholds


class LineIn(BaseModel):
    sku_id: int
    qty: int
    unit_price_paise: int
    discount_paise: int = 0


class PayIn(BaseModel):
    mode: str
    amount_paise: int
    ref: Optional[str] = None


class BillIn(BaseModel):
    id: str
    idem_key: str
    invoice_no: str
    created_at_device: str
    lines: List[LineIn]
    payments: List[PayIn]
    net_paise: int
    customer_phone: Optional[str] = None
    consent_wa: bool = False


class SyncIn(BaseModel):
    device_id: int
    bills: List[BillIn]
    queue_depth: int = 0


class SeriesIn(BaseModel):
    device_id: int
    count: int = 100


class ReturnLine(BaseModel):
    sale_line_id: int
    qty: int


class ReturnIn(BaseModel):
    invoice_no: str
    reason: str
    lines: List[ReturnLine]
    note: Optional[str] = None


class Decision(BaseModel):
    action: str


class DropIn(BaseModel):
    amount_paise: int
    business_date: str
    store_id: Optional[int] = None


class CloseIn(BaseModel):
    business_date: str
    opening_cash_paise: int = 0
    counted_cash_paise: int
    note: Optional[str] = None
    store_id: Optional[int] = None


def attach(app, ctx):
    me, need, many, one, audit, engine, an, NOW = (ctx[k] for k in ("me", "need", "many", "one", "audit", "engine", "an", "NOW"))
    clock = lambda: max(datetime.now().replace(microsecond=0), NOW)

    many_rows = lambda q, **k: [tuple(x.values()) for x in many(q, **k)]

    def policy_days():
        r = one("select value from tax_assumption where assumption_key='return_policy_days'")
        return int(r["value"]) if r else 10

    def store_of(u, store_id=None):
        if u["scope_type"] == "store": return u["scope_id"]
        if u["scope_type"] == "warehouse": raise HTTPException(403, "Warehouse users have no store counter")
        if store_id is None:
            r = one("select store_id from device order by id limit 1"); store_id = r["store_id"]
        if not one("select 1 x from device where store_id=:s", s=store_id): raise HTTPException(404, "No billing device in that store")
        return store_id

    def device_of(u, device_id):
        d = one("select d.*, s.prefix, s.fy, s.reserved_to, s.next_no from device d join invoice_series s on s.device_id=d.id where d.id=:i", i=device_id)
        if not d or (u["scope_type"] == "store" and d["store_id"] != u["scope_id"]): raise HTTPException(404, "Unknown device")
        return d

    def pdate(s):
        try: return date.fromisoformat(s)
        except Exception: raise HTTPException(422, "Bad business_date")

    # ------------------------------------------------------------------ bootstrap and series
    @app.get("/api/pos/bootstrap")
    def bootstrap(store_id: Optional[int] = None, u=Depends(need(BILL))):
        sid = store_of(u, store_id)
        store = one("select id, code, name, city from location where id=:s", s=sid)
        devices = many("select d.id, d.code, d.last_seen_at, s.prefix, s.fy, s.reserved_to, s.next_no from device d join invoice_series s on s.device_id=d.id where d.store_id=:s order by d.id", s=sid)
        cat = many("select s.id, s.code, s.barcode, s.name, s.base_uom uom, s.pack_g, s.mrp_paise, s.price_per_kg_paise, s.hsn, s.procurement_type, c.name cat, "
                   "case when s.base_uom='g' then coalesce(t.rate_loose_pct, 5.0) else coalesce(t.rate_packed_pct, 5.0) end tax_pct "
                   "from sku s join product p on p.id=s.product_id join category c on c.id=p.category_id left join tax_rate t on t.hsn=s.hsn "
                   "where s.status='active' order by c.name, s.name")
        onhand = {r[0]: r[1] for r in many_rows("select sku_id, sum(qty) from stock_balance where location_id=:s group by sku_id", s=sid)}
        for r in cat:
            r["stock"] = onhand.get(r["id"], 0)
            r["price"] = r.pop("price_per_kg_paise") if r["uom"] == "g" else r["mrp_paise"]; r.pop("mrp_paise")
        stores = many("select distinct l.id, l.code, l.name from location l join device d on d.store_id=l.id order by l.code") if u["scope_type"] == "all" else None
        note = one("select value from tax_assumption where assumption_key='gst_rates'")
        return dict(store=store, devices=devices, catalog=cat, stores=stores, return_days=policy_days(), server_time=clock().isoformat(), tax_note=note["value"] if note else None,
                    user=dict(id=u["id"], name=u["name"], role=u["role"]))

    @app.post("/api/pos/series")
    def series(b: SeriesIn, u=Depends(need(BILL))):
        d = device_of(u, b.device_id)
        n = max(1, min(b.count, BLOCK_MAX))
        with engine.begin() as c:
            last = c.execute(text("select coalesce(max(cast(substr(invoice_no, :L) as integer)), 0) from sale where device_id=:d"), dict(L=len(d["prefix"]) + 1, d=d["id"])).scalar()
            if d["reserved_to"] - last > UNUSED_CAP: raise HTTPException(409, "This device still holds too many unused invoice numbers")
            start, end = d["reserved_to"] + 1, d["reserved_to"] + n
            c.execute(text("update invoice_series set reserved_to=:e where device_id=:d"), dict(e=end, d=d["id"]))
            audit(c, u, "pos.series", "invoice_series", d["id"], dict(reserved_to=d["reserved_to"]), dict(reserved_to=end))
        return dict(device_id=d["id"], prefix=d["prefix"], fy=d["fy"], start=start, end=end)

    # ------------------------------------------------------------------ sync
    def process(c, u, d, b, cat, now):
        """Returns (status, reason). Writes the sale when accepted or flagged for review."""
        dup = c.execute(text("select invoice_no from sale where idem_key=:k"), dict(k=b.idem_key)).fetchone()
        if dup: return "duplicate", None
        if not b.lines: return "rejected", "No lines"
        if not b.invoice_no.startswith(d["prefix"]) or not b.invoice_no[len(d["prefix"]):].isdigit(): return "rejected", "Invoice number does not belong to this device series"
        n = int(b.invoice_no[len(d["prefix"]):])
        if not (1 <= n <= d["reserved_to"]): return "rejected", "Invoice number outside the reserved block"
        if c.execute(text("select 1 from sale where invoice_no=:i"), dict(i=b.invoice_no)).fetchone(): return "rejected", "Invoice number already used"
        if c.execute(text("select 1 from sale where id=:i"), dict(i=b.id)).fetchone(): return "rejected", "Bill id already used"
        try: created = datetime.fromisoformat(b.created_at_device.replace("Z", "")).replace(tzinfo=None)
        except Exception: return "rejected", "Bad bill time"
        if created > now + timedelta(days=1): return "rejected", "Bill time is in the future"
        lines, flags = [], []
        for l in b.lines:
            s = cat.get(l.sku_id)
            if not s: return "rejected", f"SKU {l.sku_id} not found"
            if l.qty <= 0 or l.unit_price_paise <= 0 or l.discount_paise < 0: return "rejected", f"Bad quantity or price on {s['code']}"
            if s["uom"] == "g" and l.qty > 50000: return "rejected", f"Weight above 50 kg on {s['code']}"
            g, net, tax = line_calc(s["uom"], l.unit_price_paise, l.qty, l.discount_paise, s["tax_pct"])
            if net < 0: return "rejected", f"Discount above line value on {s['code']}"
            if l.unit_price_paise < s["price"] * LOW_PRICE: flags.append(f"{s['code']} sold below list price")
            if g and l.discount_paise > g * BIG_DISCOUNT: flags.append(f"{s['code']} discount above 10%")
            lines.append(dict(sku_id=l.sku_id, qty=l.qty, unit=l.unit_price_paise, discount=l.discount_paise, rate=s["tax_pct"], hsn=s["hsn"], gross=g, net=net, tax=tax))
        t = bill_calc(lines)
        if abs(t["total"] - b.net_paise) > 100: return "rejected", f"Total mismatch: device {b.net_paise / 100:.2f}, server {t['total'] / 100:.2f}"
        if any(p.mode not in MODES or p.amount_paise <= 0 for p in b.payments): return "rejected", "Bad payment line"
        if sum(p.amount_paise for p in b.payments) != t["total"]: return "rejected", "Payments do not add up to the bill total"
        cust = None
        if b.customer_phone:
            ph = "".join(ch for ch in b.customer_phone if ch.isdigit())[-10:]
            if len(ph) == 10:
                r = c.execute(text("select id from customer where phone=:p"), dict(p=ph)).fetchone()
                if r: cust = r[0]
                else:
                    c.execute(text("insert into customer(code, phone, city, created_at, first_store_id, consent_wa, consent_at, loyalty_points) values (:c,:p,:ci,:t,:s,:w,:ct,0)"),
                              dict(c="W" + ph, p=ph, ci=None, t=now, s=d["store_id"], w=b.consent_wa, ct=now if b.consent_wa else None))
                    cust = c.execute(text("select id from customer where phone=:p"), dict(p=ph)).scalar()
        # stock: sell FEFO from the store balance, kits come out as their components. A shortfall flags the bill, it does not refuse it.
        today = str(now.date())
        for l in lines:
            parts = c.execute(text("select component_sku_id, qty from kit_bom where kit_sku_id=:k"), dict(k=l["sku_id"])).fetchall() or [(l["sku_id"], 1)]
            l["batch"] = None
            for comp, per in parts:
                used, miss = stockpost.take_fefo(c, now, d["store_id"], comp, per * l["qty"], today, "sale", b.invoice_no, u["id"], d["id"])
                if used and l["batch"] is None: l["batch"] = used[0][0]
                if miss: flags.append(f"{cat[l['sku_id']]['code']} short by {miss} in store stock")
        status = "review" if flags else "paid"
        c.execute(text("insert into sale(id, store_id, device_id, invoice_no, business_date, created_at_device, received_at, cashier_id, customer_id, gross_paise, discount_paise, tax_paise, net_paise, round_off_paise, status, void_reason, price_list_id, idem_key, currency) "
                       "values (:id,:st,:dv,:inv,:bd,:cd,:rc,:cu,:cs,:g,:di,:tx,:nt,:ro,:ss,:vr,null,:ik,'INR')"),
                  dict(id=b.id, st=d["store_id"], dv=d["id"], inv=b.invoice_no, bd=created.date(), cd=created, rc=now, cu=u["id"], cs=cust, g=t["gross"], di=t["discount"], tx=t["tax"], nt=t["total"],
                       ro=t["round_off"], ss=status, vr="; ".join(flags) or None, ik=b.idem_key))
        for l in lines:
            c.execute(text("insert into sale_line(sale_id, sku_id, batch_id, qty, unit_price_paise, discount_paise, tax_rate, tax_paise, net_paise, hsn) values (:s,:k,:bt,:q,:u,:d,:r,:t,:n,:h)"),
                      dict(s=b.id, bt=l["batch"], k=l["sku_id"], q=l["qty"], u=l["unit"], d=l["discount"], r=l["rate"], t=l["tax"], n=l["net"], h=l["hsn"]))
        for p in b.payments:
            c.execute(text("insert into payment(sale_id, mode, amount_paise, ref, status) values (:s,:m,:a,:r,'ok')"), dict(s=b.id, m=p.mode, a=p.amount_paise, r=p.ref))
        late = c.execute(text("select 1 from day_close where store_id=:s and business_date=:d and status!='open'"), dict(s=d["store_id"], d=created.date())).fetchone()
        if late: audit(c, u, "pos.late_bill", "sale", b.id, None, dict(invoice_no=b.invoice_no, business_date=str(created.date())))
        return status, "; ".join(flags) or None

    @app.post("/api/pos/sync")
    def sync(b: SyncIn, u=Depends(need(BILL))):
        d = device_of(u, b.device_id)
        if len(b.bills) > 50: raise HTTPException(422, "Send at most 50 bills per call")
        now = clock()
        cat = {r["id"]: r for r in many("select s.id, s.code, s.base_uom uom, s.hsn, coalesce(s.mrp_paise, s.price_per_kg_paise) price, "
                                        "case when s.base_uom='g' then coalesce(t.rate_loose_pct, 5.0) else coalesce(t.rate_packed_pct, 5.0) end tax_pct "
                                        "from sku s left join tax_rate t on t.hsn=s.hsn")}
        res = []
        with engine.begin() as c:
            started = now
            for bill in sorted(b.bills, key=lambda x: x.invoice_no):
                st, why = process(c, u, d, bill, cat, now)
                res.append(dict(idem_key=bill.idem_key, invoice_no=bill.invoice_no, status=st, reason=why))
                if st == "rejected": audit(c, u, "pos.reject", "sale", bill.id, None, dict(invoice_no=bill.invoice_no, reason=why))
            ok = sum(r["status"] in ("paid", "review") for r in res); bad = sum(r["status"] == "rejected" for r in res)
            hi = c.execute(text("select coalesce(max(cast(substr(invoice_no, :L) as integer)), 0) from sale where device_id=:d"), dict(L=len(d["prefix"]) + 1, d=d["id"])).scalar()
            c.execute(text("update invoice_series set next_no=:n where device_id=:d"), dict(n=hi + 1, d=d["id"]))
            c.execute(text("update device set last_seen_at=:t where id=:d"), dict(t=now, d=d["id"]))
            c.execute(text("insert into sync_log(device_id, started_at, finished_at, bills_pushed, bills_rejected, queue_depth, status) values (:d,:s,:f,:p,:r,:q,:st)"),
                      dict(d=d["id"], s=started, f=now, p=ok, r=bad, q=max(b.queue_depth - len(b.bills), 0), st="ok" if not bad else "partial"))
        for r in res:
            if r["status"] == "paid": r["status"] = "accepted"
        return dict(results=res, server_time=now.isoformat())

    # ------------------------------------------------------------------ bills and returns
    def bill_detail(sale):
        lines = many("select l.id, l.sku_id, s.code, s.name, s.base_uom uom, l.qty, l.unit_price_paise, l.discount_paise, l.tax_paise, l.net_paise, "
                     "coalesce((select sum(rl.qty) from sale_return_line rl join sale_return r on r.id=rl.return_id where rl.sale_line_id=l.id and r.status!='rejected'), 0) returned "
                     "from sale_line l join sku s on s.id=l.sku_id where l.sale_id=:i order by l.id", i=sale["id"])
        pays = many("select mode, amount_paise from payment where sale_id=:i", i=sale["id"])
        days = (clock().date() - sale["business_date"]).days if hasattr(sale["business_date"], "year") else (clock().date() - date.fromisoformat(str(sale["business_date"])[:10])).days
        return dict(sale, lines=lines, payments=pays, days_since_sale=days, returnable=days <= policy_days() and sale["status"] != "void")

    @app.get("/api/pos/bills")
    def bills(q: str = "", store_id: Optional[int] = None, limit: int = Query(30, le=100), u=Depends(need(BILL))):
        sid = store_of(u, store_id)
        rows = many("select id, invoice_no, business_date, created_at_device, net_paise, status, device_id from sale where store_id=:s and (:q='' or invoice_no like :lq) order by created_at_device desc limit :n",
                    s=sid, q=q.strip(), lq=f"%{q.strip()}%", n=limit)
        return dict(bills=rows)

    @app.get("/api/pos/bills/{invoice_no:path}")
    def bill(invoice_no: str, u=Depends(need(BILL))):
        s = one("select * from sale where invoice_no=:i", i=invoice_no)
        if not s or (u["scope_type"] == "store" and s["store_id"] != u["scope_id"]): raise HTTPException(404, "Bill not found")
        return bill_detail(s)

    @app.post("/api/pos/returns")
    def create_return(b: ReturnIn, u=Depends(need(BILL))):
        if b.reason not in RETURN_REASONS: raise HTTPException(422, "Returns are accepted only for defective or damaged items")
        if not b.lines: raise HTTPException(422, "Pick at least one line")
        s = one("select * from sale where invoice_no=:i", i=b.invoice_no)
        if not s or (u["scope_type"] == "store" and s["store_id"] != u["scope_id"]): raise HTTPException(404, "Bill not found")
        det = bill_detail(s)
        if s["status"] == "void": raise HTTPException(409, "Bill is void")
        if det["days_since_sale"] > policy_days(): raise HTTPException(409, f"Return window of {policy_days()} days has passed ({det['days_since_sale']} days)")
        by = {l["id"]: l for l in det["lines"]}; refund = 0; picked = []
        for rl in b.lines:
            l = by.get(rl.sale_line_id)
            if not l: raise HTTPException(422, "Line does not belong to this bill")
            if rl.qty <= 0 or rl.qty > l["qty"] - l["returned"]: raise HTTPException(422, f"Quantity on {l['code']} is above what is left to return")
            refund += (2 * l["net_paise"] * rl.qty + l["qty"]) // (2 * l["qty"]); picked.append(rl)
        manager = u["role"] in MANAGE
        with engine.begin() as c:
            n = c.execute(text("select count(*) from sale_return")).scalar() + 1
            c.execute(text("insert into sale_return(return_no, sale_id, store_id, created_at, reason, status, approved_by, refund_paise, days_since_sale) values (:r,:s,:st,:t,:re,:ss,:ab,:rf,:dy)"),
                      dict(r=f"RET-{s['store_id']}-{n:05d}", s=s["id"], st=s["store_id"], t=clock(), re=b.reason, ss="approved" if manager else "pending", ab=u["id"] if manager else None, rf=refund, dy=det["days_since_sale"]))
            rid = c.execute(text("select id from sale_return where return_no=:r"), dict(r=f"RET-{s['store_id']}-{n:05d}")).scalar()
            for rl in picked:
                c.execute(text("insert into sale_return_line(return_id, sale_line_id, qty, disposition) values (:r,:l,:q,'quarantine')"), dict(r=rid, l=rl.sale_line_id, q=rl.qty))
            audit(c, u, "pos.return", "sale_return", rid, None, dict(invoice_no=b.invoice_no, refund=refund, reason=b.reason, note=b.note))
        return dict(id=rid, return_no=f"RET-{s['store_id']}-{n:05d}", refund_paise=refund, status="approved" if manager else "pending")

    @app.get("/api/pos/returns")
    def returns(store_id: Optional[int] = None, u=Depends(need(BILL))):
        sid = store_of(u, store_id)
        return dict(returns=many("select r.id, r.return_no, r.created_at, r.reason, r.status, r.refund_paise, r.days_since_sale, s.invoice_no from sale_return r join sale s on s.id=r.sale_id where r.store_id=:s order by r.created_at desc limit 40", s=sid))

    @app.post("/api/pos/returns/{rid}/decision")
    def decide(rid: int, b: Decision, u=Depends(need(MANAGE))):
        r = one("select * from sale_return where id=:i", i=rid)
        if not r or (u["scope_type"] == "store" and r["store_id"] != u["scope_id"]): raise HTTPException(404, "Return not found")
        if r["status"] != "pending": raise HTTPException(409, "Already decided")
        if b.action not in ("approve", "reject"): raise HTTPException(422, "Action must be approve or reject")
        st = "approved" if b.action == "approve" else "rejected"
        with engine.begin() as c:
            c.execute(text("update sale_return set status=:s, approved_by=:u where id=:i"), dict(s=st, u=u["id"], i=rid))
            audit(c, u, "pos.return." + b.action, "sale_return", rid, dict(status="pending"), dict(status=st))
        return dict(status=st)

    # ------------------------------------------------------------------ cash and day close
    def cash_position(sid, bd):
        r = one("select coalesce(sum(p.amount_paise), 0) v from payment p join sale s on s.id=p.sale_id where s.store_id=:s and s.business_date=:d and s.status!='void' and p.mode='cash' and p.status='ok'", s=sid, d=bd)
        sales = r["v"]
        rf = one("select coalesce(sum(refund_paise), 0) v from sale_return where store_id=:s and status='approved' and date(created_at)=:d", s=sid, d=bd)["v"]
        dc = one("select * from day_close where store_id=:s and business_date=:d", s=sid, d=bd)
        drops = one("select coalesce(sum(amount_paise), 0) v from cash_drop where day_close_id=:i", i=dc["id"])["v"] if dc else 0
        cnt = one("select count(*) n, coalesce(sum(net_paise), 0) v from sale where store_id=:s and business_date=:d and status!='void'", s=sid, d=bd)
        mode = many("select p.mode, sum(p.amount_paise) v from payment p join sale s on s.id=p.sale_id where s.store_id=:s and s.business_date=:d and s.status!='void' group by p.mode", s=sid, d=bd)
        return dict(cash_sales=sales, refunds=rf, drops=drops, bills=cnt["n"], sales=cnt["v"], by_mode={m["mode"]: m["v"] for m in mode}, close=dc)

    @app.get("/api/pos/day-close")
    def day_close_view(business_date: str, store_id: Optional[int] = None, u=Depends(need(BILL))):
        sid = store_of(u, store_id); bd = pdate(business_date)
        p = cash_position(sid, bd)
        prev = one("select counted_cash_paise v from day_close where store_id=:s and business_date<:d and status!='open' order by business_date desc limit 1", s=sid, d=bd)
        p["suggested_opening"] = prev["v"] if prev and prev["v"] is not None else 0
        p["drop_list"] = many("select amount_paise, ts from cash_drop where day_close_id=:i order by ts", i=p["close"]["id"]) if p["close"] else []
        return p

    @app.post("/api/pos/cash-drop")
    def cash_drop(b: DropIn, u=Depends(need(BILL))):
        sid = store_of(u, b.store_id); bd = pdate(b.business_date)
        if b.amount_paise <= 0: raise HTTPException(422, "Amount must be above zero")
        with engine.begin() as c:
            dc = c.execute(text("select id, status from day_close where store_id=:s and business_date=:d"), dict(s=sid, d=bd)).fetchone()
            if dc and dc[1] != "open": raise HTTPException(409, "Day is already closed")
            if not dc:
                c.execute(text("insert into day_close(store_id, business_date, device_id, opened_by, closed_by, opening_cash_paise, expected_cash_paise, counted_cash_paise, variance_paise, status, closed_at) values (:s,:d,null,:u,null,0,0,null,null,'open',null)"), dict(s=sid, d=bd, u=u["id"]))
                dc = c.execute(text("select id, status from day_close where store_id=:s and business_date=:d"), dict(s=sid, d=bd)).fetchone()
            c.execute(text("insert into cash_drop(day_close_id, amount_paise, ts, by_user) values (:i,:a,:t,:u)"), dict(i=dc[0], a=b.amount_paise, t=clock(), u=u["id"]))
            audit(c, u, "pos.cash_drop", "cash_drop", dc[0], None, dict(amount=b.amount_paise))
        return dict(ok=True)

    @app.post("/api/pos/day-close")
    def day_close(b: CloseIn, u=Depends(need(BILL))):
        sid = store_of(u, b.store_id); bd = pdate(b.business_date)
        if b.counted_cash_paise < 0 or b.opening_cash_paise < 0: raise HTTPException(422, "Cash cannot be negative")
        p = cash_position(sid, bd)
        if p["close"] and p["close"]["status"] != "open": raise HTTPException(409, "Day is already closed")
        expected = b.opening_cash_paise + p["cash_sales"] - p["refunds"] - p["drops"]; var = b.counted_cash_paise - expected
        if abs(var) > VARIANCE_NOTE and not (b.note or "").strip(): raise HTTPException(422, "A variance above Rs 100 needs a note")
        status = "variance" if abs(var) > VARIANCE_NOTE else "closed"
        with engine.begin() as c:
            if p["close"]:
                c.execute(text("update day_close set closed_by=:u, opening_cash_paise=:o, expected_cash_paise=:e, counted_cash_paise=:c, variance_paise=:v, status=:s, closed_at=:t where id=:i"),
                          dict(u=u["id"], o=b.opening_cash_paise, e=expected, c=b.counted_cash_paise, v=var, s=status, t=clock(), i=p["close"]["id"]))
            else:
                c.execute(text("insert into day_close(store_id, business_date, device_id, opened_by, closed_by, opening_cash_paise, expected_cash_paise, counted_cash_paise, variance_paise, status, closed_at) values (:s,:d,null,:u,:u,:o,:e,:c,:v,:st,:t)"),
                          dict(s=sid, d=bd, u=u["id"], o=b.opening_cash_paise, e=expected, c=b.counted_cash_paise, v=var, st=status, t=clock()))
            audit(c, u, "pos.day_close", "day_close", f"{sid}:{bd}", None, dict(expected=expected, counted=b.counted_cash_paise, variance=var, note=b.note))
        return dict(status=status, expected_cash_paise=expected, variance_paise=var)

    # ------------------------------------------------------------------ sync health
    @app.get("/api/pos/health")
    def health(store_id: Optional[int] = None, u=Depends(need(HEALTH))):
        sc = [u["scope_id"]] if u["scope_type"] == "store" else None
        rows = many("select d.id, d.code, d.store_id, l.code store_code, l.name store_name, l.city, d.last_seen_at, "
                    "(select count(*) from sale s where s.device_id=d.id and s.received_at>=:since) bills_24h, "
                    "(select coalesce(sum(bills_rejected),0) from sync_log g where g.device_id=d.id and g.finished_at>=:since) rejected_24h, "
                    "(select queue_depth from sync_log g where g.device_id=d.id order by g.finished_at desc limit 1) queue_depth "
                    "from device d join location l on l.id=d.store_id where d.active=true and (:all=1 or d.store_id=:s)", since=NOW - timedelta(hours=24), all=0 if sc else 1, s=sc[0] if sc else 0)
        out = []
        for r in rows:
            ls = r["last_seen_at"]
            if isinstance(ls, str): ls = datetime.fromisoformat(ls)
            mins = None if ls is None else max(int((NOW - ls).total_seconds() // 60), 0)
            r["minutes_since_sync"] = mins
            r["state"] = "never" if mins is None else "ok" if mins <= FRESH_MIN or ls > NOW else "late" if mins <= LATE_H * 60 else "stale"
            out.append(r)
        order = {"stale": 0, "never": 1, "late": 2, "ok": 3}
        out.sort(key=lambda r: (order[r["state"]], -(r["minutes_since_sync"] or 0)))
        summary = {k: sum(1 for r in out if r["state"] == k) for k in order}
        return dict(summary=summary, devices=out[:60], total=len(out))
