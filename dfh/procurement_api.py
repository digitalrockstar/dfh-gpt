"""Module 3 API: price book and landed cost compare, purchase orders, shipment tracker, GRN, three-way match, vendor scorecard. Attached by dfh.app.create_app."""
import json
from datetime import timedelta, datetime, date
from typing import Optional, List, Dict
from fastapi import Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from . import stockpost
from .sql_dialect import days_between, date_plus_literal_days
from .procurement_costs import Rates, components, line_minor, weight_kg, allocate, match, FLOW, STAGE, COMPONENTS

VIEW = {"ceo", "finance", "admin", "procurement", "warehouse"}
PO_WRITE = {"ceo", "procurement", "admin", "finance"}
BIG = {"ceo", "finance", "admin"}
PO_LIMIT = 250_000_000
GRN_ROLES = {"warehouse", "procurement", "ceo", "admin"}
INV_RELEASE = {"finance", "ceo", "admin"}
INV_HOLD = {"finance", "ceo", "admin", "procurement"}
LANDED_ROLES = {"ceo", "procurement", "admin", "finance"}
OPEN = ("draft", "approved", "confirmed", "in_transit", "partial")


class Suggest(BaseModel):
    suggestion_ids: List[int]


class POAction(BaseModel):
    action: str
    expected_date: Optional[str] = None
    note: Optional[str] = None


class Book(BaseModel):
    mode: str
    etd: str
    eta: str
    port_id: Optional[int] = None
    container_no: Optional[str] = None
    bl_no: Optional[str] = None


class Event(BaseModel):
    status: str
    note: Optional[str] = None
    eta: Optional[str] = None


class Landed(BaseModel):
    components: Dict[str, float]


class GrnLine(BaseModel):
    sku_id: int
    qty_received: int
    qty_rejected: int = 0
    reject_reason: Optional[str] = None
    expiry_date: Optional[str] = None


class GrnIn(BaseModel):
    lines: List[GrnLine]


class InvIn(BaseModel):
    inv_no: str
    inv_date: str
    amount: float


class InvDecision(BaseModel):
    action: str
    note: Optional[str] = None


def attach(app, ctx):
    me, need, many, one, audit, engine, an, NOW = (ctx[k] for k in ("me", "need", "many", "one", "audit", "engine", "an", "NOW"))
    view = need(VIEW); TODAY = NOW.date()
    def rates():
        return Rates({r["assumption_key"]: r["value"] for r in many("select assumption_key, value from tax_assumption")})
    def wh(u): return [u["scope_id"]] if u["scope_type"] == "warehouse" else None
    def inl(ids): return ",".join(str(int(x)) for x in ids) or "-1"
    def inr(minor, fx): return int(minor * fx)
    def d(x): return str(x)[:10]
    def latest_fx(cur):
        if cur == "INR": return 1.0
        r = one("select rate_to_inr r from fx_rate where currency=:c order by month desc limit 1", c=cur); return r["r"] if r else 1.0
    def po_scope(u, pid):
        p = one("select p.*, v.name vendor, v.country, v.type vtype, v.currency vcur, v.payment_terms_days, l.name wh_name, l.city wh_city from purchase_order p join vendor v on v.id=p.vendor_id join location l on l.id=p.dest_location_id where p.id=:i", i=pid)
        if not p: raise HTTPException(404, "Purchase order not found")
        if wh(u) and p["dest_location_id"] not in wh(u): raise HTTPException(403, "That purchase order is outside your warehouse")
        return p
    def lines_of(pid):
        return many("select pl.id, pl.sku_id, pl.qty, pl.price_minor, s.code, s.name, s.base_uom, s.pack_g, c.code cat from purchase_order_line pl join sku s on s.id=pl.sku_id join product pr on pr.id=s.product_id join category c on c.id=pr.category_id where pl.po_id=:p order by pl.id", p=pid)
    def recv_of(pid):
        rows = many("select gl.sku_id, sum(gl.qty_received) rec, sum(gl.qty_accepted) acc, sum(gl.qty_rejected) rj from grn g join grn_line gl on gl.grn_id=g.id where g.po_id=:p group by 1", p=pid)
        return {r["sku_id"]: r for r in rows}

    # ---------- landed cost ----------
    def landed(p, ship, ls, R):
        """Landed cost for a PO. Uses recorded shipment actuals when they exist, otherwise the stated assumptions."""
        fx = p["fx_rate"]; od = d(p["order_date"])[:7]
        per = []
        for l in ls:
            row = one("select incoterm from vendor_sku_cost where vendor_id=:v and sku_id=:s and effective_from<=:d order by effective_from desc limit 1", v=p["vendor_id"], s=l["sku_id"], d=d(p["order_date"])) or {"incoterm": "CIF"}
            val = line_minor(l["qty"], l["price_minor"], l["base_uom"]) * fx; kg = weight_kg(l["qty"], l["base_uom"], l["pack_g"])
            est = components(val, kg, row["incoterm"], l["cat"], p["country"], (ship or {}).get("mode", "sea"), p["vtype"] != "import", R)
            per.append(dict(value=val, kg=kg, est=est))
        act = many("select component, amount_paise from landed_cost where shipment_id=:s", s=ship["id"]) if ship else []
        if act:
            tot = {a["component"]: a["amount_paise"] for a in act}; basis = "actual"
            sh = allocate(tot, per)
        else:
            tot = {}
            for x in per:
                for k, v in x["est"].items(): tot[k] = tot.get(k, 0) + v
            basis = "estimate"; sh = [sum(x["est"].values()) for x in per]
        goods = sum(x["value"] for x in per); extra = sum(tot.values())
        out = [dict(extra=int(e), landed=int(x["value"] + e)) for x, e in zip(per, sh)]
        return dict(basis=basis, goods=int(goods), extra=int(extra), total=int(goods + extra), premium=(extra / goods if goods else 0), components={k: int(v) for k, v in tot.items()}, lines=out)

    # ---------- match ----------
    INV = ("select i.id, i.inv_no, i.inv_date, i.due_date, i.amount_minor, i.currency, i.status, i.note, i.po_id, p.po_no, p.fx_rate, p.total_minor ordered, p.dest_location_id, v.id vendor_id, v.name vendor, "
           "(select sum(case s.base_uom when 'g' then gl.qty_accepted / 1000.0 else gl.qty_accepted * 1.0 end * pl.price_minor) from grn g join grn_line gl on gl.grn_id=g.id join purchase_order_line pl on pl.po_id=g.po_id and pl.sku_id=gl.sku_id join sku s on s.id=gl.sku_id where g.po_id=i.po_id) accepted, "
           "(select coalesce(sum(dn.amount_paise), 0) from debit_note dn join grn g on g.id=dn.grn_id where g.po_id=i.po_id) debit_paise "
           "from vendor_invoice i join purchase_order p on p.id=i.po_id join vendor v on v.id=i.vendor_id")
    def inv_rows(where="", u=None, **kw):
        R = rates(); out = []
        sc = wh(u) if u else None
        for r in many(INV + (" where " + where if where else "") + " order by i.inv_date desc, i.id desc", **kw):
            if sc and r["dest_location_id"] not in sc: continue
            m = match(r["amount_minor"], r["ordered"], int(r["accepted"] or 0), int(r["debit_paise"] / r["fx_rate"]), R.tol)
            out.append(dict(r, accepted=int(r["accepted"] or 0), debit=int(r["debit_paise"] / r["fx_rate"]), state=m["state"], variance=m["variance"], reason=m["reason"],
                            variance_inr=inr(m["variance"], r["fx_rate"]), amount_inr=inr(r["amount_minor"], r["fx_rate"]), overdue=r["status"] in ("pending", "held", "released") and d(r["due_date"]) < str(TODAY)))
        return out

    # ---------- options and summary ----------
    @app.get("/api/procurement/options")
    def options(u=Depends(view)):
        sc = wh(u)
        return dict(warehouses=many("select id, name, city, is_port from location where type='warehouse'" + (f" and id in ({inl(sc)})" if sc else "") + " order by name"),
                    vendors=many("select id, name, type from vendor where active=true order by name"), categories=many("select code, name from category order by id"),
                    can_po=u["role"] in PO_WRITE, can_receive=u["role"] in GRN_ROLES, can_release=u["role"] in INV_RELEASE, can_hold=u["role"] in INV_HOLD, can_landed=u["role"] in LANDED_ROLES,
                    po_limit=PO_LIMIT, big_roles=sorted(BIG), flow=FLOW, stage=STAGE, tolerance=rates().tol)

    def ship_sql(extra=""):
        late_days_expr = days_between(engine, ":t", "sh.eta")
        return ("select sh.id, sh.ship_no, sh.po_id, sh.mode, sh.container_no, sh.status, sh.etd, sh.eta, sh.ata, sh.bl_no, p.po_no, p.total_minor, p.fx_rate, p.status po_status, p.dest_location_id, v.name vendor, v.country, l.name wh_name, l.city wh_city, "
                f"case when sh.status!='delivered' and sh.eta < :t then {late_days_expr} else 0 end late_days, "
                "(select max(ts) from shipment_event e where e.shipment_id=sh.id) last_ts "
                "from shipment sh join purchase_order p on p.id=sh.po_id join vendor v on v.id=p.vendor_id join location l on l.id=p.dest_location_id" + extra)

    @app.get("/api/procurement/summary")
    def summary(u=Depends(view)):
        sc = wh(u); w = f" and p.dest_location_id in ({inl(sc)})" if sc else ""
        t = str(TODAY)
        op = one(f"select count(*) n, coalesce(sum(p.total_minor * p.fx_rate), 0) v from purchase_order p where p.status in ('draft','approved','confirmed','in_transit','partial'){w}")
        sh = many(ship_sql(f" where sh.status != 'delivered' and p.status != 'cancelled'{w}"), t=t)
        late = [s for s in sh if s["late_days"] > 2]
        wait = one(f"select count(*) n from shipment sh join purchase_order p on p.id=sh.po_id where sh.status='delivered' and p.status='in_transit'{w}")
        iv = inv_rows("i.status in ('pending','held','released')", u)
        todo = [i for i in iv if i["state"] == "mismatch" and i["status"] in ("pending", "held")]
        over = [i for i in iv if i["overdue"]]
        L = many(f"select sh.id, p.total_minor * p.fx_rate g, (select sum(amount_paise) from landed_cost where shipment_id=sh.id) x from shipment sh join purchase_order p on p.id=sh.po_id where sh.ata >= :a and p.currency != 'INR'{w}", a=str(TODAY - timedelta(days=90)))
        g = sum(r["g"] for r in L if r["x"]); x = sum(r["x"] for r in L if r["x"])
        return dict(open_n=op["n"], open_value=int(op["v"]), transit_n=len(sh), transit_value=int(sum(s["total_minor"] * s["fx_rate"] for s in sh)), late_n=len(late), worst_late=max([s["late_days"] for s in late], default=0),
                    awaiting_grn=wait["n"], decide_n=len(todo), decide_value=int(sum(abs(i["variance_inr"]) for i in todo)), overdue_n=len(over), overdue_value=int(sum(i["amount_inr"] for i in over)), landed_premium=(x / g if g else None), landed_n=len([r for r in L if r["x"]]))

    # ---------- purchase orders ----------
    @app.get("/api/procurement/pos")
    def pos(status: str = "", vendor: int = 0, wh_id: int = 0, page: int = 1, size: int = 15, u=Depends(view)):
        w, p = [], {}
        sc = wh(u)
        if sc: w.append(f"p.dest_location_id in ({inl(sc)})")
        if status == "open": w.append("p.status in ('draft','approved','confirmed','in_transit','partial')")
        elif status: w.append("p.status=:st"); p["st"] = status
        if vendor: w.append("p.vendor_id=:v"); p["v"] = vendor
        if wh_id: w.append("p.dest_location_id=:w"); p["w"] = wh_id
        ws = (" where " + " and ".join(w)) if w else ""; size = max(1, min(size, 50))
        tot = one("select count(*) n, coalesce(sum(p.total_minor * p.fx_rate), 0) v from purchase_order p" + ws, **p)
        items = many("select p.id, p.po_no, p.status, p.order_date, p.expected_date, p.currency, p.fx_rate, p.total_minor, v.name vendor, v.country, l.name wh_name, l.city, "
                     "(select status from shipment where po_id=p.id order by id desc limit 1) ship_status, (select count(*) from purchase_order_line where po_id=p.id) n_lines "
                     "from purchase_order p join vendor v on v.id=p.vendor_id join location l on l.id=p.dest_location_id" + ws + " order by p.order_date desc, p.id desc limit :lim offset :off", **p, lim=size, off=(max(1, page) - 1) * size)
        for i in items: i["value_inr"] = inr(i["total_minor"], i["fx_rate"]); i["late"] = bool(i["status"] in ("confirmed", "in_transit", "partial") and d(i["expected_date"]) < str(TODAY - timedelta(days=2)) and i["ship_status"] != "delivered")
        return dict(total=tot["n"], value=int(tot["v"]), page=page, size=size, items=items)

    @app.get("/api/procurement/pos/{pid}")
    def po_detail(pid: int, u=Depends(view)):
        p = po_scope(u, pid); R = rates(); ls = lines_of(pid); rc = recv_of(pid)
        ship = one("select * from shipment where po_id=:p order by id desc limit 1", p=pid)
        lc = landed(p, ship, ls, R)
        for l, x in zip(ls, lc["lines"]):
            r = rc.get(l["sku_id"], {}); l.update(value_minor=line_minor(l["qty"], l["price_minor"], l["base_uom"]), received=int(r.get("rec") or 0), accepted=int(r.get("acc") or 0), rejected=int(r.get("rj") or 0),
                                                   landed_unit=x["landed"] / l["qty"] if l["qty"] else 0, extra_unit=x["extra"] / l["qty"] if l["qty"] else 0)
        events = many("select status, ts, note from shipment_event where shipment_id=:s order by ts, id", s=ship["id"]) if ship else []
        grns = many("select g.id, g.grn_no, g.received_at, g.status, l.name wh_name from grn g join location l on l.id=g.location_id where g.po_id=:p order by g.id", p=pid)
        for g in grns: g["lines"] = many("select gl.sku_id, s.name, s.base_uom, gl.qty_received, gl.qty_accepted, gl.qty_rejected, gl.reject_reason, b.batch_no, b.expiry_date from grn_line gl join sku s on s.id=gl.sku_id left join batch b on b.id=gl.batch_id where gl.grn_id=:g", g=g["id"])
        inv = inv_rows("i.po_id=:p", None, p=pid); sh_flow = FLOW.get(ship["mode"]) if ship else None
        r = u["role"]; st = p["status"]; inr_total = inr(p["total_minor"], p["fx_rate"]); acts = []
        if r in PO_WRITE:
            if st == "draft": acts.append("approve")
            if st == "approved": acts.append("confirm")
            if st in ("draft", "approved", "confirmed") and not grns: acts.append("cancel")
            if st in ("approved", "confirmed") and not ship: acts.append("book")
        if r in GRN_ROLES and st in ("confirmed", "in_transit", "partial"): acts.append("receive")
        if r in INV_HOLD and not inv and st in ("confirmed", "in_transit", "partial", "received"): acts.append("invoice")
        return dict(po=dict(p, value_inr=inr_total, needs_big=inr_total > PO_LIMIT and r not in BIG), lines=ls, shipment=ship, flow=sh_flow, events=events, landed=dict(lc, lines=None), grns=grns, invoice=inv[0] if inv else None, actions=acts,
                    debit_notes=many("select dn.amount_paise, dn.reason, dn.created_at from debit_note dn join grn g on g.id=dn.grn_id where g.po_id=:p", p=pid))

    @app.post("/api/procurement/pos/from-suggestions")
    def from_suggestions(b: Suggest, u=Depends(need(PO_WRITE))):
        if not b.suggestion_ids: raise HTTPException(422, "Pick at least one suggestion")
        sg = many(f"select id, location_id, sku_id, vendor_id, approved_qty, status, po_id from reorder_suggestion where id in ({inl(b.suggestion_ids)})")
        if len(sg) != len(set(b.suggestion_ids)): raise HTTPException(404, "A suggestion was not found")
        for s in sg:
            if s["status"] != "approved" or s["po_id"]: raise HTTPException(409, "Only approved suggestions that are not on a PO can be ordered")
            if not s["vendor_id"]: raise HTTPException(422, "A suggestion has no vendor")
        groups = {}
        for s in sg: groups.setdefault((s["vendor_id"], s["location_id"]), []).append(s)
        made = []
        with engine.begin() as c:
            nxt = c.execute(text("select coalesce(max(id), 0) from purchase_order")).scalar()
            for (v, loc), items in groups.items():
                vd = one("select currency from vendor where id=:v", v=v); fx = latest_fx(vd["currency"]); lines, lead = [], 0
                for s in items:
                    row = one("select price_minor, lead_time_days from vendor_sku_cost where vendor_id=:v and sku_id=:s order by effective_from desc limit 1", v=v, s=s["sku_id"])
                    if not row: raise HTTPException(422, "No price book entry for one of the lines")
                    uom = one("select base_uom from sku where id=:i", i=s["sku_id"])["base_uom"]; lead = max(lead, row["lead_time_days"])
                    lines.append((s, row["price_minor"], uom))
                total = sum(line_minor(s["approved_qty"], pm, uom) for s, pm, uom in lines); nxt += 1
                c.execute(text("insert into purchase_order(id, po_no, vendor_id, dest_location_id, status, currency, fx_rate, order_date, expected_date, total_minor, notes) values (:i,:n,:v,:l,'draft',:c,:f,:o,:e,:t,:no)"),
                          dict(i=nxt, n=f"PO-{TODAY:%y%m}-{nxt:04d}", v=v, l=loc, c=vd["currency"], f=fx, o=TODAY, e=TODAY + timedelta(days=lead), t=total, no="Created from approved buy suggestions"))
                for s, pm, uom in lines:
                    c.execute(text("insert into purchase_order_line(po_id, sku_id, qty, price_minor) values (:p,:s,:q,:pm)"), dict(p=nxt, s=s["sku_id"], q=s["approved_qty"], pm=pm))
                    c.execute(text("update reorder_suggestion set po_id=:p, status='ordered' where id=:i"), dict(p=nxt, i=s["id"]))
                audit(c, u, "create", "purchase_order", nxt, None, dict(source="suggestions", lines=len(lines), total_minor=total)); made.append(dict(id=nxt, po_no=f"PO-{TODAY:%y%m}-{nxt:04d}", lines=len(lines)))
        return dict(created=made)

    def bump_order(c, p, sign):
        for l in lines_of(p["id"]):
            row = c.execute(text("select on_order from stock_position where location_id=:l and sku_id=:s"), dict(l=p["dest_location_id"], s=l["sku_id"])).first()
            if row: c.execute(text("update stock_position set on_order=:o where location_id=:l and sku_id=:s"), dict(o=max(0, row[0] + sign * l["qty"]), l=p["dest_location_id"], s=l["sku_id"]))

    @app.post("/api/procurement/pos/{pid}/action")
    def po_action(pid: int, b: POAction, u=Depends(need(PO_WRITE))):
        p = po_scope(u, pid); st = p["status"]; inr_total = inr(p["total_minor"], p["fx_rate"]); new = None
        if b.action == "approve":
            if st != "draft": raise HTTPException(409, f"PO is {st}")
            if inr_total > PO_LIMIT and u["role"] not in BIG: raise HTTPException(403, "POs above ₹25 lakh need the CEO, finance or admin to approve")
            new = "approved"
        elif b.action == "confirm":
            if st != "approved": raise HTTPException(409, "Only an approved PO can be confirmed")
            new = "confirmed"
        elif b.action == "cancel":
            if st not in ("draft", "approved", "confirmed"): raise HTTPException(409, f"A {st} PO cannot be cancelled")
            if one("select 1 x from grn where po_id=:p", p=pid): raise HTTPException(409, "Goods are already received against this PO")
            if not (b.note or "").strip(): raise HTTPException(422, "Say why it is cancelled")
            new = "cancelled"
        else: raise HTTPException(422, "Action must be approve, confirm or cancel")
        with engine.begin() as c:
            ex = None
            if b.action == "confirm" and b.expected_date:
                try: ex = date.fromisoformat(b.expected_date)
                except ValueError: raise HTTPException(422, "Use a date like 2026-11-20")
            c.execute(text("update purchase_order set status=:s, approved_by=coalesce(:a, approved_by), expected_date=coalesce(:e, expected_date) where id=:i"), dict(s=new, a=u["id"] if b.action == "approve" else None, e=ex, i=pid))
            if b.action == "approve": bump_order(c, p, 1)
            if b.action == "cancel":
                if st in ("approved", "confirmed"): bump_order(c, p, -1)
                c.execute(text("update reorder_suggestion set po_id=null, status='approved' where po_id=:p"), dict(p=pid))
            audit(c, u, b.action, "purchase_order", pid, dict(status=st), dict(status=new, note=b.note))
        return dict(id=pid, status=new)

    # ---------- shipments ----------
    @app.get("/api/procurement/shipments")
    def shipments(view_: str = "transit", page: int = 1, size: int = 12, u=Depends(view)):
        sc = wh(u); w = f" and p.dest_location_id in ({inl(sc)})" if sc else ""
        cond = {"transit": "sh.status != 'delivered' and p.status != 'cancelled'", "late": "sh.status != 'delivered' and sh.eta < :lt and p.status != 'cancelled'", "receive": "sh.status='delivered' and p.status='in_transit'", "all": "1=1"}.get(view_)
        if cond is None: raise HTTPException(422, "Unknown view")
        t = str(TODAY); lt = str(TODAY - timedelta(days=2))
        rows = many(ship_sql(f" where {cond}{w} order by case when sh.status!='delivered' then 0 else 1 end, late_days desc, sh.eta limit :lim offset :off"), t=t, lt=lt, lim=max(1, min(size, 50)), off=(max(1, page) - 1) * size)
        tot = one(f"select count(*) n from shipment sh join purchase_order p on p.id=sh.po_id where {cond}{w}", lt=lt)["n"]
        for r in rows:
            fl = FLOW[r["mode"]]; r["step"] = fl.index(r["status"]); r["steps"] = len(fl); r["value_inr"] = inr(r["total_minor"], r["fx_rate"])
            stuck_days_expr = days_between(engine, ":t", "max(ts)")
            r["stuck"] = r["status"] == "customs" and one(f"select {stuck_days_expr} d from shipment_event where shipment_id=:s and status='customs'", t=str(TODAY), s=r["id"])["d"] or 0
        return dict(total=tot, items=rows)

    @app.post("/api/procurement/pos/{pid}/shipment")
    def book(pid: int, b: Book, u=Depends(need(PO_WRITE))):
        p = po_scope(u, pid)
        if p["status"] not in ("approved", "confirmed"): raise HTTPException(409, "Book a shipment once the PO is approved")
        if one("select 1 x from shipment where po_id=:p and status!='delivered'", p=pid): raise HTTPException(409, "This PO already has a shipment in progress")
        if b.mode not in FLOW: raise HTTPException(422, "Mode must be sea, air or road")
        if (b.mode == "road") != (p["vtype"] != "import"): raise HTTPException(422, "Domestic vendors ship by road and imports by sea or air")
        try: etd, eta = date.fromisoformat(b.etd), date.fromisoformat(b.eta)
        except ValueError: raise HTTPException(422, "Use dates like 2026-11-20")
        if eta < etd: raise HTTPException(422, "ETA is before the departure date")
        if b.mode != "road":
            pt = one("select is_port from location where id=:i and type='warehouse'", i=b.port_id) if b.port_id else None
            if not pt or not pt["is_port"]: raise HTTPException(422, "Pick the port of arrival")
        with engine.begin() as c:
            n = c.execute(text("select coalesce(max(id), 0) + 1 from shipment")).scalar()
            c.execute(text("insert into shipment(id, ship_no, po_id, mode, container_no, port_id, status, etd, eta, bl_no) values (:i,:n,:p,:m,:c,:pt,'booked',:e,:a,:b)"),
                      dict(i=n, n=f"SH-{TODAY:%y%m}-{n:04d}", p=pid, m=b.mode, c=b.container_no, pt=b.port_id if b.mode != "road" else None, e=etd, a=eta, b=b.bl_no))
            c.execute(text("insert into shipment_event(shipment_id, status, ts, note) values (:s,'booked',:t,null)"), dict(s=n, t=NOW))
            c.execute(text("update purchase_order set status='confirmed' where id=:i and status='approved'"), dict(i=pid))
            audit(c, u, "book", "shipment", n, None, dict(po_id=pid, mode=b.mode, eta=b.eta))
        return dict(id=n)

    @app.post("/api/procurement/shipments/{sid}/event")
    def ship_event(sid: int, b: Event, u=Depends(need(PO_WRITE))):
        s = one("select sh.*, p.dest_location_id, p.status po_status from shipment sh join purchase_order p on p.id=sh.po_id where sh.id=:i", i=sid)
        if not s: raise HTTPException(404, "Shipment not found")
        fl = FLOW[s["mode"]]
        if b.status not in fl: raise HTTPException(422, f"Status must be one of {', '.join(fl)}")
        if fl.index(b.status) <= fl.index(s["status"]): raise HTTPException(409, f"Shipment is already {STAGE[s['status']].lower()}. Moves only go forward")
        if b.status == "customs" and not (b.note or "").strip(): pass
        ne = None
        if b.eta:
            try: ne = date.fromisoformat(b.eta)
            except ValueError: raise HTTPException(422, "Use a date like 2026-11-20")
        with engine.begin() as c:
            c.execute(text("update shipment set status=:s, eta=coalesce(:e, eta), ata=case when :s='at_port' or (:s='delivered' and mode='road') then coalesce(ata, :t) else ata end where id=:i"), dict(s=b.status, e=ne, t=TODAY, i=sid))
            c.execute(text("insert into shipment_event(shipment_id, status, ts, note) values (:i,:s,:t,:n)"), dict(i=sid, s=b.status, t=NOW, n=b.note))
            if fl.index(b.status) >= 2: c.execute(text("update purchase_order set status='in_transit' where id=:p and status in ('approved','confirmed')"), dict(p=s["po_id"]))
            audit(c, u, "shipment_event", "shipment", sid, dict(status=s["status"]), dict(status=b.status, note=b.note, eta=b.eta))
        return dict(id=sid, status=b.status)

    @app.post("/api/procurement/shipments/{sid}/landed")
    def set_landed(sid: int, b: Landed, u=Depends(need(LANDED_ROLES))):
        s = one("select sh.id, p.currency, p.fx_rate from shipment sh join purchase_order p on p.id=sh.po_id where sh.id=:i", i=sid)
        if not s: raise HTTPException(404, "Shipment not found")
        if not b.components: raise HTTPException(422, "Nothing to record")
        for k, v in b.components.items():
            if k not in COMPONENTS: raise HTTPException(422, f"Unknown cost: {k}")
            if v < 0: raise HTTPException(422, "Costs cannot be negative")
        with engine.begin() as c:
            old = {r[0]: r[1] for r in c.execute(text("select component, amount_paise from landed_cost where shipment_id=:s"), dict(s=sid))}
            for k, v in b.components.items():
                c.execute(text("delete from landed_cost where shipment_id=:s and component=:k"), dict(s=sid, k=k))
                c.execute(text("insert into landed_cost(shipment_id, component, amount_paise, currency, fx_rate) values (:s,:k,:a,'INR',1.0)"), dict(s=sid, k=k, a=int(round(v * 100))))
            audit(c, u, "landed_cost", "shipment", sid, old, {k: int(round(v * 100)) for k, v in b.components.items()})
        return dict(id=sid, components=len(b.components))

    # ---------- GRN ----------
    @app.post("/api/procurement/pos/{pid}/grn")
    def grn(pid: int, b: GrnIn, u=Depends(need(GRN_ROLES))):
        p = po_scope(u, pid)
        if p["status"] not in ("confirmed", "in_transit", "partial"): raise HTTPException(409, f"Goods cannot be received against a {p['status']} PO")
        ls = {l["sku_id"]: l for l in lines_of(pid)}; rc = recv_of(pid); seen = set(); out = []
        if not b.lines: raise HTTPException(422, "Add at least one line")
        for x in b.lines:
            l = ls.get(x.sku_id)
            if not l: raise HTTPException(422, "A line is not on this PO")
            if x.sku_id in seen: raise HTTPException(422, "A SKU appears twice")
            seen.add(x.sku_id)
            if x.qty_received <= 0: continue
            if x.qty_rejected < 0 or x.qty_rejected > x.qty_received: raise HTTPException(422, f"Rejected quantity is more than received for {l['name']}")
            got = int((rc.get(x.sku_id) or {}).get("rec") or 0)
            if got + x.qty_received > l["qty"] * 1.05: raise HTTPException(422, f"{l['name']}: more than 5% over the ordered quantity")
            if x.qty_rejected and not (x.reject_reason or "").strip(): raise HTTPException(422, f"Give a reason for the rejection on {l['name']}")
            acc = x.qty_received - x.qty_rejected; ex = None
            if acc > 0:
                try: ex = date.fromisoformat(x.expiry_date or "")
                except ValueError: raise HTTPException(422, f"Enter the expiry date for {l['name']}")
                if ex <= TODAY: raise HTTPException(422, f"{l['name']} is already expired")
            out.append((l, x, acc, ex))
        if not out: raise HTTPException(422, "Nothing received")
        ship = one("select * from shipment where po_id=:p order by id desc limit 1", p=pid); R = rates(); lc = landed(p, ship, list(ls.values()), R)
        unit = {l["id"]: x["landed"] / l["qty"] for l, x in zip(list(ls.values()), lc["lines"])}
        with engine.begin() as c:
            gid = c.execute(text("select coalesce(max(id), 0) + 1 from grn")).scalar(); wc = one("select code from location where id=:i", i=p["dest_location_id"])["code"]
            gno = f"GRN-{wc[7:].replace('-', '')}-{gid:05d}"
            c.execute(text("insert into grn(id, grn_no, po_id, shipment_id, location_id, received_at, status, received_by) values (:i,:n,:p,:s,:l,:t,'posted',:u)"), dict(i=gid, n=gno, p=pid, s=ship["id"] if ship else None, l=p["dest_location_id"], t=NOW, u=u["id"]))
            for l, x, acc, ex in out:
                bid = None; uc = int(round(unit[l["id"]]))
                if acc > 0:
                    bid = c.execute(text("select coalesce(max(id), 0) + 1 from batch")).scalar()
                    c.execute(text("insert into batch(id, batch_no, sku_id, vendor_id, received_on, mfg_date, expiry_date, unit_cost_paise, source_doc_type, source_doc_id) values (:i,:n,:s,:v,:r,null,:e,:u,'grn',:g)"), dict(i=bid, n=f"{gno}-{l['sku_id']}", s=l["sku_id"], v=p["vendor_id"], r=TODAY, e=ex, u=uc, g=gno))
                if bid: stockpost.post(c, NOW, p["dest_location_id"], l["sku_id"], bid, acc, "grn", "grn", gno, u["id"])
                c.execute(text("insert into grn_line(grn_id, sku_id, batch_id, qty_received, qty_accepted, qty_rejected, reject_reason, unit_cost_paise) values (:g,:s,:b,:r,:a,:j,:rs,:u)"), dict(g=gid, s=l["sku_id"], b=bid, r=x.qty_received, a=acc, j=x.qty_rejected, rs=(x.reject_reason or None) if x.qty_rejected else None, u=uc))
                if x.qty_rejected:
                    c.execute(text("insert into debit_note(vendor_id, grn_id, amount_paise, reason, created_at) values (:v,:g,:a,:r,:t)"), dict(v=p["vendor_id"], g=gid, a=inr(line_minor(x.qty_rejected, l["price_minor"], l["base_uom"]), p["fx_rate"]), r=f"Rejected on receipt: {x.reject_reason}", t=NOW))
                row = c.execute(text("select on_hand, on_order from stock_position where location_id=:l and sku_id=:s"), dict(l=p["dest_location_id"], s=l["sku_id"])).first()
                if row: c.execute(text("update stock_position set on_hand=:h, on_order=:o where location_id=:l and sku_id=:s"), dict(h=row[0] + acc, o=max(0, row[1] - x.qty_received), l=p["dest_location_id"], s=l["sku_id"]))
                else: c.execute(text("insert into stock_position(location_id, sku_id, on_hand, on_order, eta_days, as_of) values (:l,:s,:h,0,0,:d)"), dict(l=p["dest_location_id"], s=l["sku_id"], h=acc, d=TODAY))
            tot = {r[0]: r[1] for r in c.execute(text("select gl.sku_id, sum(gl.qty_received) from grn g join grn_line gl on gl.grn_id=g.id where g.po_id=:p group by 1"), dict(p=pid))}
            full = all(tot.get(s, 0) >= l["qty"] * .95 for s, l in ls.items()); new = "received" if full else "partial"
            c.execute(text("update purchase_order set status=:s where id=:i"), dict(s=new, i=pid))
            if ship and ship["status"] != "delivered":
                c.execute(text("update shipment set status='delivered', ata=coalesce(ata, :t) where id=:i"), dict(t=TODAY, i=ship["id"]))
                c.execute(text("insert into shipment_event(shipment_id, status, ts, note) values (:i,'delivered',:t,'Delivered at the GRN')"), dict(i=ship["id"], t=NOW))
            audit(c, u, "grn", "grn", gid, None, dict(po_id=pid, lines=len(out), status=new))
        return dict(id=gid, grn_no=gno, po_status=new)

    # ---------- invoices and match ----------
    @app.get("/api/procurement/match")
    def match_list(state: str = "decide", page: int = 1, size: int = 12, u=Depends(view)):
        rows = inv_rows("i.status in ('pending','held')", u)
        sel = {"decide": lambda r: r["state"] == "mismatch", "ready": lambda r: r["state"] == "matched", "awaiting": lambda r: r["state"] == "awaiting", "all": lambda r: True}.get(state)
        if not sel: raise HTTPException(422, "Unknown state")
        counts = {k: sum(1 for r in rows if f(r)) for k, f in (("decide", lambda r: r["state"] == "mismatch"), ("ready", lambda r: r["state"] == "matched"), ("awaiting", lambda r: r["state"] == "awaiting"), ("all", lambda r: True))}
        pick = sorted([r for r in rows if sel(r)], key=lambda r: -abs(r["variance_inr"]))
        return dict(total=len(pick), counts=counts, at_risk=int(sum(abs(r["variance_inr"]) for r in rows if r["state"] == "mismatch")), items=pick[(page - 1) * size:page * size])

    @app.post("/api/procurement/pos/{pid}/invoice")
    def add_invoice(pid: int, b: InvIn, u=Depends(need(INV_HOLD))):
        p = po_scope(u, pid)
        if p["status"] in ("draft", "approved", "cancelled"): raise HTTPException(409, "Invoices come after the vendor has shipped")
        if one("select 1 x from vendor_invoice where po_id=:p", p=pid): raise HTTPException(409, "This PO already has an invoice")
        if b.amount <= 0: raise HTTPException(422, "Amount must be positive")
        if one("select 1 x from vendor_invoice where vendor_id=:v and inv_no=:n", v=p["vendor_id"], n=b.inv_no.strip()): raise HTTPException(409, "That invoice number is already recorded for this vendor")
        try: idt = date.fromisoformat(b.inv_date)
        except ValueError: raise HTTPException(422, "Use a date like 2026-11-20")
        if idt > TODAY: raise HTTPException(422, "Invoice date is in the future")
        with engine.begin() as c:
            n = c.execute(text("select coalesce(max(id), 0) + 1 from vendor_invoice")).scalar()
            c.execute(text("insert into vendor_invoice(id, vendor_id, po_id, inv_no, inv_date, amount_minor, currency, status, due_date) values (:i,:v,:p,:n,:d,:a,:c,'pending',:due)"),
                      dict(i=n, v=p["vendor_id"], p=pid, n=b.inv_no.strip(), d=idt, a=int(round(b.amount * 100)), c=p["currency"], due=idt + timedelta(days=p["payment_terms_days"])))
            audit(c, u, "create", "vendor_invoice", n, None, dict(po_id=pid, amount_minor=int(round(b.amount * 100))))
        return dict(id=n)

    @app.post("/api/procurement/invoices/{iid}/decision")
    def decide(iid: int, b: InvDecision, u=Depends(need(INV_HOLD))):
        rows = inv_rows("i.id=:i", None, i=iid)
        if not rows: raise HTTPException(404, "Invoice not found")
        r = rows[0]
        if r["status"] not in ("pending", "held"): raise HTTPException(409, f"Invoice is already {r['status']}")
        note = (b.note or "").strip()
        if b.action == "release":
            if u["role"] not in INV_RELEASE: raise HTTPException(403, "Only finance, the CEO or admin can release an invoice")
            if r["state"] == "awaiting": raise HTTPException(409, "Goods have not been received against this PO")
            if r["state"] == "mismatch" and len(note) < 5: raise HTTPException(422, "Releasing a mismatch needs a note saying why")
            new = "released"
        elif b.action == "hold":
            if len(note) < 5: raise HTTPException(422, "Say why it is on hold")
            if r["status"] == "held": raise HTTPException(409, "Already on hold")
            new = "held"
        elif b.action == "dispute":
            if r["state"] != "mismatch" or r["variance"] <= 0: raise HTTPException(409, "Only an over-billed mismatch can be disputed")
            if len(note) < 5: raise HTTPException(422, "Say what is being disputed")
            new = "disputed"
        else: raise HTTPException(422, "Action must be release, hold or dispute")
        with engine.begin() as c:
            c.execute(text("update vendor_invoice set status=:s, note=:n where id=:i"), dict(s=new, n=note or r["note"], i=iid))
            if new == "disputed":
                g = c.execute(text("select max(id) from grn where po_id=:p"), dict(p=r["po_id"])).scalar()
                c.execute(text("insert into debit_note(vendor_id, grn_id, amount_paise, reason, created_at) values (:v,:g,:a,:r,:t)"), dict(v=r["vendor_id"], g=g, a=abs(r["variance_inr"]), r=f"Over-billed on {r['inv_no']}: {r['reason']}", t=NOW))
            audit(c, u, b.action, "vendor_invoice", iid, dict(status=r["status"], state=r["state"]), dict(status=new, note=note))
        return dict(id=iid, status=new)

    # ---------- price book and compare ----------
    def book_rows():
        R = rates()
        rows = many("select c.vendor_id, c.sku_id, c.currency, c.price_minor, c.price_per, c.incoterm, c.moq_qty, c.lead_time_days, c.effective_from ef, v.name vname, v.country, v.type vtype from vendor_sku_cost c join vendor v on v.id=c.vendor_id where c.effective_from >= '2026-07-01' order by c.effective_from")
        return R, rows

    def per_unit(R, c, sku, fx):
        """Landed cost per kg for g SKUs, per unit for ea SKUs, in paise."""
        goods = c["price_minor"] * fx; kg = 1.0 if sku["base_uom"] == "g" else (sku["pack_g"] or 0) / 1000.0
        comp = components(goods, kg, c["incoterm"], sku["cat"], c["country"], "sea", c["vtype"] != "import", R)
        return goods, comp, goods + sum(comp.values())

    SKUQ = "select s.id, s.code, s.name, s.base_uom, s.pack_g, c.code cat, c.name cat_name from sku s join product p on p.id=s.product_id join category c on c.id=p.category_id"

    @app.get("/api/procurement/pricebook")
    def pricebook(q: str = "", cat: str = "", only: str = "", page: int = 1, size: int = 12, u=Depends(view)):
        R, rows = book_rows(); skus = {s["id"]: s for s in many(SKUQ)}; fx = {}
        for r in many("select currency, rate_to_inr r from fx_rate where month=(select max(month) from fx_rate)"): fx[r["currency"]] = r["r"]
        by = {}
        for r in rows: by.setdefault((r["sku_id"], r["vendor_id"]), []).append(r)
        agg = {}
        for (s, v), rs in by.items():
            rs.sort(key=lambda x: str(x["ef"])); cur = rs[-1]; old = rs[0]; sk = skus.get(s)
            if not sk: continue
            _, _, land = per_unit(R, dict(cur, country=cur["country"], vtype=cur["vtype"]), sk, fx.get(cur["currency"], 1.0))
            chg = cur["price_minor"] / old["price_minor"] - 1 if old["price_minor"] else 0
            if sk["base_uom"] == "g": land /= 1.0
            agg.setdefault(s, []).append(dict(vendor=cur["vname"], vid=v, land=land, chg=chg))
        items = []
        for s, vs in agg.items():
            sk = skus[s]
            if cat and sk["cat"] != cat: continue
            if q and q.lower() not in (sk["name"] + " " + sk["code"]).lower(): continue
            vs.sort(key=lambda x: x["land"]); best, worst = vs[0], vs[-1]; creep = [x for x in vs if x["chg"] >= .10]
            if only == "creep" and not creep: continue
            items.append(dict(id=s, code=sk["code"], name=sk["name"], cat=sk["cat_name"], uom=sk["base_uom"], vendors=len(vs), best=best["vendor"], best_landed=best["land"], worst_landed=worst["land"], spread=(worst["land"] / best["land"] - 1) if best["land"] else 0,
                              creep=[dict(vendor=x["vendor"], change=x["chg"]) for x in creep]))
        items.sort(key=lambda x: (-len(x["creep"]), -x["spread"]) if only == "creep" else -x["spread"])
        return dict(total=len(items), creep_skus=sum(1 for i in items if i["creep"]), items=items[(page - 1) * size:page * size], unit_note="Landed cost is paise per kg for loose SKUs and per unit for packs, at the latest month's FX and the stated cost assumptions.")

    @app.get("/api/procurement/compare/{sid}")
    def compare(sid: int, u=Depends(view)):
        R = rates(); sk = one(SKUQ + " where s.id=:i", i=sid)
        if not sk: raise HTTPException(404, "SKU not found")
        rows = many("select c.vendor_id, c.currency, c.price_minor, c.price_per, c.incoterm, c.moq_qty, c.lead_time_days, c.effective_from ef, v.name vname, v.country, v.type vtype from vendor_sku_cost c join vendor v on v.id=c.vendor_id where c.sku_id=:s order by c.effective_from", s=sid)
        if not rows: raise HTTPException(404, "No vendor prices for this SKU")
        fx = {(r["currency"], str(r["month"])[:7]): r["rate_to_inr"] for r in many("select currency, month, rate_to_inr from fx_rate")}
        by = {}
        for r in rows: by.setdefault(r["vendor_id"], []).append(r)
        out = []
        for v, rs in by.items():
            cur = rs[-1]; f = fx.get((cur["currency"], str(cur["ef"])[:7]), 1.0) if cur["currency"] != "INR" else 1.0
            goods, comp, land = per_unit(R, cur, sk, f)
            series = [r["price_minor"] * (fx.get((r["currency"], str(r["ef"])[:7]), 1.0) if r["currency"] != "INR" else 1.0) for r in rs]
            own = [r["price_minor"] for r in rs]; ch = own[-1] / own[-4] - 1 if len(own) >= 4 and own[-4] else None
            out.append(dict(vendor_id=v, vendor=cur["vname"], country=cur["country"], currency=cur["currency"], incoterm=cur["incoterm"], price_minor=cur["price_minor"], moq=cur["moq_qty"], lead=cur["lead_time_days"], goods=goods, components={k: int(x) for k, x in comp.items()}, landed=land, series=series, change_3m=ch))
        out.sort(key=lambda x: x["landed"])
        for i, o in enumerate(out): o["rank"] = i + 1; o["vs_best"] = o["landed"] / out[0]["landed"] - 1
        recent = many("select p.po_no, p.order_date, v.name vendor, pl.qty, pl.price_minor, p.fx_rate, p.currency from purchase_order_line pl join purchase_order p on p.id=pl.po_id join vendor v on v.id=p.vendor_id where pl.sku_id=:s and p.status != 'cancelled' order by p.order_date desc limit 5", s=sid)
        return dict(sku=sk, unit="kg" if sk["base_uom"] == "g" else "unit", vendors=out, recent=recent, note="Landed cost is goods at the latest month's FX plus freight, insurance, duty, surcharge, clearing, port and haulage at the stated assumptions. CIF prices already include freight and insurance. Import GST is left out because it is recoverable.")

    # ---------- vendor scorecard ----------
    @app.get("/api/procurement/vendors")
    def vendors(u=Depends(view)):
        base = {r["id"]: r for r in many("select v.id, v.name, v.country, v.type, count(p.id) pos, coalesce(sum(p.total_minor * p.fx_rate), 0) value from vendor v join purchase_order p on p.vendor_id=v.id where p.status != 'cancelled' group by v.id")}
        eta_plus_two = date_plus_literal_days(engine, "sh.eta", 2)
        ot = {r["vendor_id"]: r for r in many(f"select p.vendor_id, count(*) n, sum(case when sh.ata <= {eta_plus_two} then 1 else 0 end) ok from shipment sh join purchase_order p on p.id=sh.po_id where sh.ata is not null group by 1")}
        fl = {r["vendor_id"]: r for r in many("select p.vendor_id, sum(gl.qty_received) rec, sum(gl.qty_rejected) rj, sum(pl.qty) ord from grn g join purchase_order p on p.id=g.po_id join grn_line gl on gl.grn_id=g.id join purchase_order_line pl on pl.po_id=p.id and pl.sku_id=gl.sku_id group by 1")}
        pay = {}
        for i in inv_rows("i.status in ('pending','held','released')", u): pay[i["vendor_id"]] = pay.get(i["vendor_id"], 0) + i["amount_inr"]
        rows = many("select vendor_id, sku_id, price_minor, effective_from ef from vendor_sku_cost where effective_from in ('2026-07-01', '2026-10-01')")
        pr = {}
        for r in rows: pr.setdefault((r["vendor_id"], r["sku_id"]), {})[str(r["ef"])[5:7]] = r["price_minor"]
        up = {}
        for (v, s), m in pr.items():
            if "07" in m and "10" in m and m["07"]: up.setdefault(v, []).append(m["10"] / m["07"] - 1)
        out = []
        for v, b in base.items():
            o = ot.get(v); f = fl.get(v); ch = up.get(v, []); avg = sum(ch) / len(ch) if ch else None; raised = sum(1 for x in ch if x >= .05)
            r = dict(b, value=int(b["value"]), on_time=(o["ok"] / o["n"] if o and o["n"] >= 3 else None), shipments=o["n"] if o else 0, fill=(f["rec"] / f["ord"] if f and f["ord"] else None), reject=(f["rj"] / f["rec"] if f and f["rec"] else None), price_3m=avg, payable=int(pay.get(v, 0)), flags=[])
            if avg is not None and avg >= .10 and raised >= 5: r["flags"].append("Price creep")
            if r["on_time"] is not None and r["on_time"] < .7: r["flags"].append("Late")
            if r["fill"] is not None and r["fill"] < .95: r["flags"].append("Short supply")
            if r["reject"] is not None and r["reject"] >= .03: r["flags"].append("Rejects")
            out.append(r)
        out.sort(key=lambda x: (-len(x["flags"]), -x["value"]))
        return dict(items=out[:25], total=len(out), flagged=sum(1 for x in out if x["flags"]), note="On time means arrival within 2 days of the ETA. Fill is quantity received over quantity ordered. Price change is the average move in the vendor's own currency price, July to October, across its SKUs.")

    def exceptions(f):
        out = []
        if f["store_ids"] is not None: return out
        t = str(TODAY)
        for s in many(ship_sql(" where sh.status='customs' and p.status != 'cancelled'"), t=t):
            customs_days_expr = days_between(engine, ":t", "max(ts)")
            days = one(f"select {customs_days_expr} d from shipment_event where shipment_id=:s and status='customs'", t=t, s=s["id"])["d"] or 0
            if days >= 5: out.append(dict(type="customs", severity="high", title=f"{s['ship_no']} has been in customs for {days} days", detail=f"{s['vendor']} to {s['wh_name']}, worth {inr(s['total_minor'], s['fx_rate']) / 1e7:.2f} Cr. ETA was {d(s['eta'])}. Demurrage and storage charges build up daily.", ref=s["po_no"]))
        late = [s for s in many(ship_sql(" where sh.status != 'delivered' and sh.eta < :lt and p.status != 'cancelled'"), t=t, lt=str(TODAY - timedelta(days=7))) if s["status"] != "customs"]
        if late:
            top = {}
            for s in late: top[s["vendor"]] = top.get(s["vendor"], 0) + 1
            nm = sorted(top.items(), key=lambda x: -x[1])[0]
            out.append(dict(type="late_shipments", severity="med", title=f"{len(late)} shipments are more than 7 days past their ETA", detail=f"Most from {nm[0]} ({nm[1]}). Open Procurement and logistics, then Late.", ref="late"))
        todo = [i for i in inv_rows("i.status in ('pending','held')") if i["state"] == "mismatch"]
        if todo:
            tot = sum(abs(i["variance_inr"]) for i in todo)
            out.append(dict(type="match", severity="med", title=f"{len(todo)} vendor invoices do not match goods received", detail=f"{tot / 1e5:.1f} lakh of difference. Largest: {sorted(todo, key=lambda i: -abs(i['variance_inr']))[0]['vendor']}.", ref="match"))
        v = [x for x in vendors({"role": "ceo", "scope_type": "all", "scope_id": None})["items"] if "Price creep" in x["flags"]]
        if v: out.append(dict(type="creep", severity="high", title=f"{v[0]['name']} has raised prices {v[0]['price_3m'] * 100:.0f}% since July", detail=f"Average across its SKUs, in its own currency. Other vendors on the same SKUs are flat. See the price book.", ref="creep"))
        return out

    return dict(exceptions=exceptions)
