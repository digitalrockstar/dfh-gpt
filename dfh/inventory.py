"""Module 4 API: stock, batches and expiry, transfers, indents, repack, cycle counts, kit readiness, shrink.
Rules: stock only moves through post(), which writes the ledger and the balance together and refuses to go below zero.
Picking is FEFO (earliest expiry first, expired batches never ship). Allocated indents reserve stock without moving it."""
import json
from datetime import timedelta
from typing import List, Optional
from fastapi import Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from .sql_dialect import days_between

READ = {"ceo", "finance", "admin", "procurement", "warehouse", "store_manager"}
WRITE = {"ceo", "admin", "warehouse"}
LINE_LIMIT, PLAN_LIMIT, EWAY_LIMIT, MAX_LOSS_PCT = 500_000, 1_000_000, 5_000_000, 5.0     # paise, paise, paise, percent
FEST_FACTOR = 4                                                                              # festival stocking assumption: stores need 4x their usual max level of a gift box
ACTIONS = [(0, "Quarantine, write off through wastage"), (15, "Markdown 30 percent now and move to high street stores"), (30, "Markdown 15 percent and push in gift mixes"), (60, "Watch closely and hold fresh receipts")]


def val(q, c, uom="s.base_uom"): return f"(case when {uom}='g' then {q}/1000.0 else {q}*1.0 end*{c})"
def inlist(ids): return "(" + ",".join(str(int(i)) for i in ids) + ")"


class Ln(BaseModel):
    sku_id: int
    qty: int
class TrIn(BaseModel):
    from_id: int
    to_id: int
    lines: List[Ln]
    vehicle_no: Optional[str] = None
class DispIn(BaseModel):
    vehicle_no: Optional[str] = None
    eway_ref: Optional[str] = None
class RecvLn(BaseModel):
    line_id: int
    qty_received: int
class RecvIn(BaseModel):
    lines: List[RecvLn]
class IndIn(BaseModel):
    store_id: Optional[int] = None
    needed_by: Optional[str] = None
    lines: List[Ln]
class RepOut(BaseModel):
    sku_id: int
    units: int
class RepIn(BaseModel):
    warehouse_id: int
    bulk_sku_id: int
    batch_id: int
    input_g: int
    outputs: List[RepOut]
class CountIn(BaseModel):
    location_id: int
    abc_class: str
    size: int = 15
class CountLn(BaseModel):
    line_id: int
    counted_qty: int
class CountSub(BaseModel):
    lines: List[CountLn]


def register(app, engine, one, many, me, need, audit, NOW):
    TODAY = str(NOW.date())
    inv_read, inv_over = need(READ), need(READ - {"store_manager"})
    err = lambda code, msg: HTTPException(code, msg)

    def allowed(u):
        if u["scope_type"] == "warehouse": return {u["scope_id"], *[r["id"] for r in many("select id from location where serving_wh_id=:w", w=u["scope_id"])]}
        if u["scope_type"] == "store": return {u["scope_id"]}
        return None

    def see(u, loc):
        a = allowed(u)
        if a is not None and loc not in a: raise err(403, "That location is outside your scope")

    def mover(u, loc):
        if u["role"] not in WRITE: raise err(403, "Your role cannot move stock")
        if u["scope_type"] == "warehouse" and loc != u["scope_id"]: raise err(403, "You can only act for your own warehouse")

    def loc_row(lid):
        r = one("select id, code, name, type, city, state from location where id=:i", i=lid)
        if not r: raise err(404, "Location not found")
        return r

    def post(c, ts, loc, sid, bid, qty, mt, dtp, did, uid=None):
        cost = c.execute(text("select unit_cost_paise from batch where id=:b"), dict(b=bid)).scalar()
        cur = c.execute(text("select qty from stock_balance where location_id=:l and sku_id=:s and batch_id=:b"), dict(l=loc, s=sid, b=bid)).scalar()
        new = (cur or 0) + qty
        if new < 0: raise err(422, "Not enough stock in that batch")
        c.execute(text("insert into stock_ledger(ts, location_id, sku_id, batch_id, qty, movement_type, doc_type, doc_id, unit_cost_paise, user_id) values (:t,:l,:s,:b,:q,:m,:d,:i,:c,:u)"),
                  dict(t=ts, l=loc, s=sid, b=bid, q=qty, m=mt, d=dtp, i=did, c=cost, u=uid))
        if cur is None: c.execute(text("insert into stock_balance(location_id, sku_id, batch_id, qty, updated_at) values (:l,:s,:b,:q,:t)"), dict(l=loc, s=sid, b=bid, q=new, t=ts))
        else: c.execute(text("update stock_balance set qty=:q, updated_at=:t where location_id=:l and sku_id=:s and batch_id=:b"), dict(l=loc, s=sid, b=bid, q=new, t=ts))

    def batches_at(c, loc, sid, skip_indent=None, fresh_only=True):
        rows = c.execute(text("select b.batch_id, b.qty, bt.expiry_date, bt.batch_no from stock_balance b join batch bt on bt.id=b.batch_id where b.location_id=:l and b.sku_id=:s and b.qty>0 "
                              + ("and bt.expiry_date>=:d " if fresh_only else "") + "order by bt.expiry_date, b.batch_id"), dict(l=loc, s=sid, d=TODAY)).fetchall()
        held = dict(c.execute(text("select p.batch_id, sum(p.qty) from pick p join indent_line il on il.id=p.indent_line_id join indent i on i.id=il.indent_id where i.status='allocated' and i.warehouse_id=:l and il.sku_id=:s and p.picked_at is null "
                                   + ("and i.id<>:x " if skip_indent else "") + "group by p.batch_id"), dict(l=loc, s=sid, x=skip_indent or 0)).fetchall())
        return [(r[0], r[1] - (held.get(r[0]) or 0), r[2], r[3]) for r in rows if r[1] - (held.get(r[0]) or 0) > 0]

    def fefo(c, loc, sid, qty, skip_indent=None):
        out, left = [], qty
        for bid, av, exp, bn in batches_at(c, loc, sid, skip_indent):
            take = min(av, left); out.append((bid, take)); left -= take
            if left <= 0: break
        return out, max(0, left)

    def check_qty(sku, qty):
        if qty <= 0: raise err(422, f"Quantity must be positive for {sku['name']}")
        if sku["base_uom"] == "g" and qty % 100: raise err(422, f"{sku['name']} moves in multiples of 100 g")

    def sku_map(ids):
        rows = many(f"select id, name, base_uom, status, std_cost_paise, procurement_type from sku where id in {inlist(ids)}") if ids else []
        return {r["id"]: r for r in rows}

    def merge(lines):
        d = {}
        for ln in lines: d[ln.sku_id] = d.get(ln.sku_id, 0) + ln.qty
        return d

    def value_of(c, lines):
        tot = 0
        for bid, sid, qty in lines:
            r = c.execute(text("select s.base_uom, bt.unit_cost_paise from batch bt join sku s on s.id=bt.sku_id where bt.id=:b"), dict(b=bid)).fetchone()
            tot += (qty / 1000.0 if r[0] == "g" else qty) * r[1]
        return int(tot)

    def eway_rule(src, dst, value, ref):
        if src["state"] != dst["state"] and value > EWAY_LIMIT and not (ref or "").strip():
            raise err(422, "An e-way reference is needed for inter-state moves above ₹50,000 (stated assumption, GST integration is on hold)")

    # ---------------------------------------------------------------- locations and overview
    @app.get("/api/inv/locations")
    def locations(type: str = "warehouse", q: str = "", u=Depends(inv_read)):
        a = allowed(u); w, p = ["l.type=:t"], dict(t=type)
        if q: w.append("(lower(l.name) like :q or lower(l.code) like :q or lower(l.city) like :q)"); p["q"] = f"%{q.lower()}%"
        if a is not None: w.append(f"l.id in {inlist(a)}")
        return many("select l.id, l.code, l.name, l.type, l.city, l.format, l.tier from location l where " + " and ".join(w) + " order by l.code limit 60", **p)

    @app.get("/api/inv/overview")
    def overview(u=Depends(inv_over)):
        a = allowed(u); lim90 = str((NOW - timedelta(days=90)).date()); lim30 = str((NOW.date() + timedelta(days=30)))
        whs = many("select id, code, name, city from location where type='warehouse' order by code")
        if a is not None: whs = [w for w in whs if w["id"] in a]
        ids = [w["id"] for w in whs] or [0]
        stock = {r["id"]: r for r in many(f"select b.location_id id, sum({val('b.qty', 'bt.unit_cost_paise')}) value, count(distinct b.sku_id) skus from stock_balance b join sku s on s.id=b.sku_id join batch bt on bt.id=b.batch_id where b.location_id in {inlist(ids)} and b.qty>0 group by 1")}
        near = {r["id"]: r["v"] for r in many(f"select b.location_id id, sum({val('b.qty', 'bt.unit_cost_paise')}) v from stock_balance b join sku s on s.id=b.sku_id join batch bt on bt.id=b.batch_id where b.location_id in {inlist(ids)} and b.qty>0 and bt.expiry_date<=:d group by 1", d=lim30)}
        shr = {r["id"]: r for r in many(f"select sl.location_id id, sum(case when sl.movement_type='shrink' then {val('-sl.qty', 'sl.unit_cost_paise')} else 0 end) shrink, sum(case when sl.movement_type='shrink' and sl.doc_id='unexplained' then {val('-sl.qty', 'sl.unit_cost_paise')} else 0 end) unexplained, "
                                         f"sum(case when sl.movement_type='receipt' then {val('sl.qty', 'sl.unit_cost_paise')} else 0 end) received from stock_ledger sl join sku s on s.id=sl.sku_id where sl.location_id in {inlist(ids)} and sl.ts>=:d and sl.movement_type in ('shrink','receipt') group by 1", d=lim90)}
        repk = {r["id"]: r for r in many(f"select warehouse_id id, sum(input_g) inp, sum(loss_g) loss from repack_job where warehouse_id in {inlist(ids)} group by 1")}
        transit = {r["id"]: r["v"] for r in many(f"select t.from_location_id id, sum({val('tl.qty_sent', 'bt.unit_cost_paise')}) v from transfer t join transfer_line tl on tl.transfer_id=t.id join sku s on s.id=tl.sku_id join batch bt on bt.id=tl.batch_id where t.status='in_transit' and t.from_location_id in {inlist(ids)} group by 1")}
        opened = {r["id"]: r["n"] for r in many(f"select warehouse_id id, count(*) n from indent where status in ('open','allocated') and warehouse_id in {inlist(ids)} group by 1")}
        low = {r["wh"]: r for r in many("select m.wh, count(*) tracked, sum(case when coalesce(h.q,0) < m.mn then 1 else 0 end) low from (select st.serving_wh_id wh, sa.sku_id, sum(sa.min_qty) mn from store_assortment sa join location st on st.id=sa.store_id group by 1,2) m "
                                         "left join (select b.location_id wh, b.sku_id, sum(b.qty) q from stock_balance b join location l on l.id=b.location_id and l.type='warehouse' group by 1,2) h on h.wh=m.wh and h.sku_id=m.sku_id group by m.wh")}
        rows = []
        for w in whs:
            s, sh, rp, lw = stock.get(w["id"], {}), shr.get(w["id"], {}), repk.get(w["id"], {}), low.get(w["id"], {})
            rec = sh.get("received") or 0
            rows.append(dict(w, value=int(s.get("value") or 0), skus=s.get("skus") or 0, near30=int(near.get(w["id"]) or 0), shrink90=int(sh.get("shrink") or 0), unexplained90=int(sh.get("unexplained") or 0),
                             shrink_rate=(sh.get("shrink") or 0) / rec if rec else 0, repack_loss=(rp["loss"] / rp["inp"]) if rp.get("inp") else None, in_transit=int(transit.get(w["id"]) or 0),
                             open_indents=opened.get(w["id"], 0), low_skus=lw.get("low") or 0, tracked_skus=lw.get("tracked") or 0))
        exc, rates = [], sorted(r["shrink_rate"] for r in rows)
        med = rates[len(rates) // 2] if rates else 0
        for r in rows:
            if med > 0 and r["shrink_rate"] > 3 * med and r["shrink_rate"] > 0.005:
                share = r["unexplained90"] / r["shrink90"] if r["shrink90"] else 0
                exc.append(dict(type="shrink", severity="high", ref=r["id"], title=f"{r['name']} is losing {r['shrink_rate'] * 100:.1f}% of received stock to shrink",
                                detail=f"{r['shrink_rate'] / med:.1f}x the typical warehouse over 90 days. {share * 100:.0f}% of the loss has no reason code." + (f" Repack yield loss is {r['repack_loss'] * 100:.1f}%." if r["repack_loss"] else "")))
            lows = sorted(x["low_skus"] for x in rows)
            if r["low_skus"] >= 4 and r["low_skus"] > 3 * lows[len(lows) // 2] + 2:
                top = many("select s.name from (select sa.sku_id, sum(sa.min_qty) mn from store_assortment sa join location st on st.id=sa.store_id where st.serving_wh_id=:w group by 1) m join sku s on s.id=m.sku_id "
                           "left join (select sku_id, sum(qty) q from stock_balance where location_id=:w group by 1) h on h.sku_id=m.sku_id where coalesce(h.q,0) < m.mn order by (m.mn - coalesce(h.q,0)) * s.popularity desc limit 3", w=r["id"])
                exc.append(dict(type="cover", severity="high", ref=r["id"], title=f"{r['name']} is under store minimums on {r['low_skus']} SKUs", detail="Most affected: " + ", ".join(x["name"] for x in top) + "."))
        if u["role"] != "warehouse":
            kits = kit_rows()
            by = {}
            for k in kits:
                if k["short"]: by.setdefault(k["limiter"]["name"], []).append(k)
            for comp, ks in sorted(by.items(), key=lambda x: -len(x[1]))[:2]:
                exc.append(dict(type="kit", severity="high", ref=ks[0]["id"], title=f"{comp} limits {len(ks)} gift box{'es' if len(ks) > 1 else ''}",
                                detail=f"Warehouse stock builds {sum(k['buildable'] for k in ks):,} boxes against {sum(k['gap'] for k in ks):,} still needed across stores. Example: {ks[0]['name']}."))
        for r in many(f"select s.id, s.name, count(distinct b.location_id) locs, sum({val('b.qty', 'bt.unit_cost_paise')}) v, min(bt.expiry_date) first_exp from stock_balance b join location l on l.id=b.location_id and l.format='Mall Kiosk' join batch bt on bt.id=b.batch_id join sku s on s.id=b.sku_id "
                      "where b.qty>0 and bt.expiry_date<=:d and bt.expiry_date>=:t" + ("" if a is None else f" and b.location_id in {inlist(a)}") + " group by s.id having locs>=5 order by v desc limit 2", d=str(NOW.date() + timedelta(days=45)), t=TODAY):
            exc.append(dict(type="expiry", severity="med", ref=r["id"], title=f"{r['name']} expires soon in {r['locs']} kiosks", detail=f"About {r['v'] / 100:,.0f} rupees at cost, first expiry {r['first_exp']}. See the Expiry tab."))
        stale = many("select count(*) n from transfer where status='in_transit' and dispatched_at<:d", d=NOW - timedelta(days=2))[0]["n"]
        if stale: exc.append(dict(type="transit", severity="med", ref=None, title=f"{stale} transfer{'s' if stale > 1 else ''} in transit for more than 2 days", detail="Stock is off the source books and not yet on the destination books."))
        order = {"high": 0, "med": 1}
        return dict(warehouses=rows, exceptions=sorted(exc, key=lambda x: order[x["severity"]]), median_shrink_rate=med,
                    totals=dict(value=sum(r["value"] for r in rows), near30=sum(r["near30"] for r in rows), in_transit=sum(r["in_transit"] for r in rows), shrink90=sum(r["shrink90"] for r in rows)))

    # ---------------------------------------------------------------- stock, batches, ledger
    @app.get("/api/inv/stock")
    def stock(location_id: int, q: str = "", cat: str = "", flag: str = "", sort: str = "value", page: int = 1, size: int = 50, u=Depends(inv_read)):
        see(u, location_id); L = loc_row(location_id)
        ref = ("select sku_id, min_qty mn, max_qty mx from store_assortment where store_id=:l" if L["type"] == "store" else
               "select sa.sku_id, sum(sa.min_qty) mn, sum(sa.max_qty) mx from store_assortment sa join location st on st.id=sa.store_id where st.serving_wh_id=:l group by sa.sku_id")
        rows = many(f"select s.id, s.code, s.name, c.code cat, c.name cat_name, s.base_uom, s.status, coalesce(h.qty,0) qty, coalesce(h.batches,0) batches, h.first_exp, coalesce(h.value,0) value, m.mn min_qty, m.mx max_qty, "
                    "case when h.first_exp is not null then {days_between(engine, "h.first_exp", ":d")} end days_left from sku s join product p on p.id=s.product_id join category c on c.id=p.category_id "
                    f"left join (select b.sku_id, sum(b.qty) qty, count(*) batches, min(bt.expiry_date) first_exp, sum({val('b.qty', 'bt.unit_cost_paise', 'sx.base_uom')}) value from stock_balance b join batch bt on bt.id=b.batch_id join sku sx on sx.id=b.sku_id where b.location_id=:l and b.qty>0 group by b.sku_id) h on h.sku_id=s.id "
                    f"left join ({ref}) m on m.sku_id=s.id where h.sku_id is not null or m.mn is not null", l=location_id, d=TODAY)
        for r in rows:
            r["state"] = "out" if r["qty"] == 0 else "low" if r["min_qty"] is not None and r["qty"] < r["min_qty"] else "near" if r["days_left"] is not None and r["days_left"] <= 30 else "ok"
        tot = dict(value=int(sum(r["value"] for r in rows)), skus=sum(1 for r in rows if r["qty"] > 0), out=sum(1 for r in rows if r["state"] == "out"), low=sum(1 for r in rows if r["state"] == "low"),
                   near=sum(1 for r in rows if r["days_left"] is not None and r["days_left"] <= 30 and r["qty"] > 0))
        if q: rows = [r for r in rows if q.lower() in r["name"].lower() or q.lower() in r["code"].lower()]
        if cat: rows = [r for r in rows if r["cat"] == cat]
        if flag == "low": rows = [r for r in rows if r["state"] in ("low", "out")]
        elif flag == "near": rows = [r for r in rows if r["days_left"] is not None and r["days_left"] <= 30 and r["qty"] > 0]
        elif flag == "out": rows = [r for r in rows if r["state"] == "out"]
        keys = {"value": lambda r: -r["value"], "qty": lambda r: -r["qty"], "name": lambda r: r["name"], "expiry": lambda r: (r["days_left"] is None, r["days_left"] or 0)}
        rows.sort(key=keys.get(sort, keys["value"])); size = max(1, min(size, 200))
        return dict(location=L, totals=tot, total=len(rows), page=page, size=size, items=rows[(page - 1) * size: page * size])

    @app.get("/api/inv/sku/{sid}")
    def sku_stock(sid: int, u=Depends(inv_read)):
        s = one("select s.id, s.code, s.name, s.base_uom, s.shelf_life_days, c.name cat_name from sku s join product p on p.id=s.product_id join category c on c.id=p.category_id where s.id=:i", i=sid)
        if not s: raise err(404, "SKU not found")
        a = allowed(u); lf = "" if a is None else f" and b.location_id in {inlist(a)}"
        s["locations"] = many(f"select l.id, l.code, l.name, l.type, sum(b.qty) qty, sum({val('b.qty', 'bt.unit_cost_paise')}) value, min(bt.expiry_date) first_exp from stock_balance b join location l on l.id=b.location_id join batch bt on bt.id=b.batch_id join sku s on s.id=b.sku_id "
                              f"where b.sku_id=:i and b.qty>0{lf} group by l.id order by l.type, qty desc limit 60", i=sid)
        s["batches"] = many(f"select bt.id, bt.batch_no, bt.received_on, bt.expiry_date, bt.unit_cost_paise, {days_between(engine, "bt.expiry_date", ":d")} days_left, sum(b.qty) qty, count(*) locs from batch bt join stock_balance b on b.batch_id=bt.id where bt.sku_id=:i and b.qty>0{lf} group by bt.id order by bt.expiry_date", i=sid, d=TODAY)
        return s

    def ledger_q(w, p, page, size):
        wh = " where " + " and ".join(w) if w else ""
        total = one("select count(*) n from stock_ledger sl" + wh, **p)["n"]
        items = many("select sl.id, sl.ts, l.code loc_code, l.name loc_name, s.name sku_name, s.base_uom, bt.batch_no, sl.qty, sl.movement_type, sl.doc_type, sl.doc_id, sl.unit_cost_paise, usr.name by_user from stock_ledger sl join location l on l.id=sl.location_id join sku s on s.id=sl.sku_id "
                     "left join batch bt on bt.id=sl.batch_id left join app_user usr on usr.id=sl.user_id" + wh + " order by sl.ts desc, sl.id desc limit :lim offset :off", **p, lim=size, off=(page - 1) * size)
        return dict(total=total, page=page, size=size, items=items)

    @app.get("/api/inv/ledger")
    def ledger(sku_id: int = 0, location_id: int = 0, batch_id: int = 0, movement: str = "", page: int = 1, size: int = 40, u=Depends(inv_read)):
        w, p, a = [], {}, allowed(u)
        for col, v, k in (("sl.sku_id", sku_id, "s"), ("sl.location_id", location_id, "l"), ("sl.batch_id", batch_id, "b")):
            if v: w.append(f"{col}=:{k}"); p[k] = v
        if movement: w.append("sl.movement_type=:m"); p["m"] = movement
        if location_id: see(u, location_id)
        elif a is not None: w.append(f"sl.location_id in {inlist(a)}")
        if not (sku_id or location_id or batch_id): raise err(422, "Give a SKU, a location or a batch")
        return ledger_q(w, p, max(1, page), max(1, min(size, 200)))

    @app.get("/api/inv/expiry")
    def expiry(days: int = 60, location_id: int = 0, cat: str = "", u=Depends(inv_read)):
        a = allowed(u); lim = str(NOW.date() + timedelta(days=min(days, 365))); w, p = ["b.qty>0", "bt.expiry_date<=:lim"], dict(lim=lim, d=TODAY)
        if location_id: see(u, location_id); w.append("b.location_id=:l"); p["l"] = location_id
        elif a is not None: w.append(f"b.location_id in {inlist(a)}")
        if cat: w.append("c.code=:cat"); p["cat"] = cat
        base = ("from stock_balance b join batch bt on bt.id=b.batch_id join sku s on s.id=b.sku_id join product pr on pr.id=s.product_id join category c on c.id=pr.category_id join location l on l.id=b.location_id where " + " and ".join(w))
        dl = days_between(engine, "bt.expiry_date", ":d")
        rows = many(f"select bt.id batch_id, bt.batch_no, s.id sku_id, s.name, c.name cat_name, s.base_uom, bt.expiry_date, {dl} days_left, sum(b.qty) qty, count(*) locs, sum(case when l.format='Mall Kiosk' then 1 else 0 end) kiosks, sum({val('b.qty', 'bt.unit_cost_paise')}) value {base} group by bt.id order by bt.expiry_date, value desc limit 300", **p)
        for r in rows: r["action"] = next(t for lim_, t in ACTIONS if r["days_left"] <= lim_) if r["days_left"] <= 60 else ACTIONS[-1][1]
        bk = many(f"select sum(case when {dl}<0 then {val('b.qty', 'bt.unit_cost_paise')} else 0 end) expired, sum(case when {dl} between 0 and 15 then {val('b.qty', 'bt.unit_cost_paise')} else 0 end) d15, sum(case when {dl} between 16 and 30 then {val('b.qty', 'bt.unit_cost_paise')} else 0 end) d30, "
                  f"sum(case when {dl} between 31 and 60 then {val('b.qty', 'bt.unit_cost_paise')} else 0 end) d60 {base}", **p)[0]
        return dict(buckets={k: int(v or 0) for k, v in bk.items()}, items=rows)

    # ---------------------------------------------------------------- transfers
    def tr_scope(u):
        a = allowed(u)
        return "" if a is None else f" and (t.from_location_id in {inlist(a)} or t.to_location_id in {inlist(a)})"

    @app.get("/api/inv/transfers")
    def transfers(status: str = "", q: str = "", page: int = 1, size: int = 30, u=Depends(inv_read)):
        w, p = [], {}
        if status: w.append("t.status=:st"); p["st"] = status
        if q: w.append("(lower(t.transfer_no) like :q or lower(f.name) like :q or lower(d.name) like :q)"); p["q"] = f"%{q.lower()}%"
        wh = "where 1=1" + "".join(" and " + x for x in w) + tr_scope(u)
        total = one("select count(*) n from transfer t join location f on f.id=t.from_location_id join location d on d.id=t.to_location_id " + wh, **p)["n"]
        items = many("select t.id, t.transfer_no, t.status, t.created_at, t.dispatched_at, t.received_at, t.vehicle_no, t.eway_ref, t.indent_id, f.code from_code, f.name from_name, d.code to_code, d.name to_name, d.type to_type, "
                     "(select count(*) from transfer_line tl where tl.transfer_id=t.id) lines, "
                     f"(select sum({val('tl.qty_sent', 'coalesce(bt.unit_cost_paise, sx.std_cost_paise)', 'sx.base_uom')}) from transfer_line tl join sku sx on sx.id=tl.sku_id left join batch bt on bt.id=tl.batch_id where tl.transfer_id=t.id) value, "
                     "(select count(*) from transfer_line tl where tl.transfer_id=t.id and tl.qty_received is not null and tl.qty_received<tl.qty_sent) short_lines "
                     "from transfer t join location f on f.id=t.from_location_id join location d on d.id=t.to_location_id " + wh + " order by coalesce(t.dispatched_at, t.created_at) desc limit :lim offset :off", **p, lim=size, off=(page - 1) * size)
        return dict(total=total, page=page, size=size, items=items)

    def tr_get(tid, u):
        t = one("select t.*, f.code from_code, f.name from_name, f.state from_state, d.code to_code, d.name to_name, d.type to_type, d.state to_state from transfer t join location f on f.id=t.from_location_id join location d on d.id=t.to_location_id where t.id=:i", i=tid)
        if not t: raise err(404, "Transfer not found")
        a = allowed(u)
        if a is not None and t["from_location_id"] not in a and t["to_location_id"] not in a: raise err(403, "That transfer is outside your scope")
        return t

    @app.get("/api/inv/transfers/{tid}")
    def transfer_detail(tid: int, u=Depends(inv_read)):
        t = tr_get(tid, u)
        t["lines"] = many("select tl.id, tl.sku_id, s.name, s.code, s.base_uom, bt.batch_no, bt.expiry_date, tl.qty_sent, tl.qty_received from transfer_line tl join sku s on s.id=tl.sku_id left join batch bt on bt.id=tl.batch_id where tl.transfer_id=:i order by tl.id", i=tid)
        t["ledger"] = many("select sl.ts, sl.movement_type, sl.qty, l.code loc_code, s.name sku_name from stock_ledger sl join location l on l.id=sl.location_id join sku s on s.id=sl.sku_id where sl.doc_type='transfer' and sl.doc_id=:n order by sl.id limit 60", n=t["transfer_no"])
        return t

    @app.post("/api/inv/transfers")
    def transfer_create(b: TrIn, u=Depends(inv_read)):
        src, dst = loc_row(b.from_id), loc_row(b.to_id); mover(u, b.from_id)
        if src["type"] != "warehouse": raise err(422, "Transfers start at a warehouse")
        if b.from_id == b.to_id: raise err(422, "Source and destination are the same")
        if not b.lines or len(b.lines) > 100: raise err(422, "Add between 1 and 100 lines")
        want = merge(b.lines); sk = sku_map(list(want))
        for sid, qty in want.items():
            if sid not in sk: raise err(422, f"Unknown SKU {sid}")
            if sk[sid]["status"] == "draft": raise err(422, f"{sk[sid]['name']} is still a draft SKU")
            check_qty(sk[sid], qty)
        with engine.begin() as c:
            n = c.execute(text("select coalesce(max(id),0)+1 from transfer")).scalar()
            c.execute(text("insert into transfer(transfer_no, from_location_id, to_location_id, status, created_at, vehicle_no) values (:n,:f,:t,'draft',:c,:v)"), dict(n=f"TR-26{n:04d}", f=b.from_id, t=b.to_id, c=NOW, v=(b.vehicle_no or None)))
            tid = c.execute(text("select id from transfer where transfer_no=:n"), dict(n=f"TR-26{n:04d}")).scalar()
            for sid, qty in want.items(): c.execute(text("insert into transfer_line(transfer_id, sku_id, qty_sent) values (:t,:s,:q)"), dict(t=tid, s=sid, q=qty))
            audit(c, u, "transfer_create", "transfer", tid, None, dict(from_id=b.from_id, to_id=b.to_id, lines=len(want)))
        return transfer_detail(tid, u)

    def run_dispatch(c, u, t, picks, vehicle, eway, indent_id=None):
        """picks: list of (batch_id, sku_id, qty). Writes ledger, lines and status."""
        src, dst = loc_row(t["from_location_id"]), loc_row(t["to_location_id"])
        if not (vehicle or "").strip(): raise err(422, "A vehicle number is needed to dispatch")
        eway_rule(src, dst, value_of(c, picks), eway)
        for bid, sid, qty in picks: post(c, NOW, src["id"], sid, bid, -qty, "transfer_out", "transfer", t["transfer_no"], u["id"])
        c.execute(text("delete from transfer_line where transfer_id=:t"), dict(t=t["id"]))
        for bid, sid, qty in picks: c.execute(text("insert into transfer_line(transfer_id, sku_id, batch_id, qty_sent) values (:t,:s,:b,:q)"), dict(t=t["id"], s=sid, b=bid, q=qty))
        c.execute(text("update transfer set status='in_transit', dispatched_at=:d, vehicle_no=:v, eway_ref=:e where id=:i"), dict(d=NOW, v=vehicle.strip(), e=(eway or "").strip() or None, i=t["id"]))

    @app.post("/api/inv/transfers/{tid}/dispatch")
    def transfer_dispatch(tid: int, b: DispIn, u=Depends(inv_read)):
        t = tr_get(tid, u); mover(u, t["from_location_id"])
        if t["status"] != "draft": raise err(422, f"Only draft transfers can be dispatched, this one is {t['status']}")
        with engine.begin() as c:
            lines = c.execute(text("select sku_id, qty_sent from transfer_line where transfer_id=:t order by id"), dict(t=tid)).fetchall(); sk = sku_map([r[0] for r in lines]); picks, short = [], []
            for sid, qty in lines:
                got, left = fefo(c, t["from_location_id"], sid, qty)
                if left:
                    stale = sum(x[1] for x in batches_at(c, t["from_location_id"], sid, fresh_only=False)) - sum(x[1] for x in batches_at(c, t["from_location_id"], sid))
                    short.append(f"{sk[sid]['name']} (need {qty}, can ship {qty - left}" + (f", {stale} is expired" if stale > 0 else "") + ")")
                picks += [(bid, sid, q) for bid, q in got]
            if short: raise err(422, "Not enough shippable stock: " + "; ".join(short))
            run_dispatch(c, u, t, picks, b.vehicle_no or t["vehicle_no"], b.eway_ref or t["eway_ref"])
            audit(c, u, "transfer_dispatch", "transfer", tid, dict(status="draft"), dict(status="in_transit", lines=len(picks)))
        return transfer_detail(tid, u)

    @app.post("/api/inv/transfers/{tid}/receive")
    def transfer_receive(tid: int, b: RecvIn, u=Depends(inv_read)):
        t = tr_get(tid, u)
        if u["role"] not in WRITE and not (u["role"] == "store_manager" and t["to_location_id"] == u["scope_id"]): raise err(403, "Your role cannot receive this transfer")
        if u["scope_type"] == "warehouse" and t["to_location_id"] != u["scope_id"]: raise err(403, "Only the destination can receive it")
        if t["status"] != "in_transit": raise err(422, f"Only transfers in transit can be received, this one is {t['status']}")
        got = {x.line_id: x.qty_received for x in b.lines}
        with engine.begin() as c:
            lines = c.execute(text("select tl.id, tl.sku_id, tl.batch_id, tl.qty_sent from transfer_line tl where tl.transfer_id=:t"), dict(t=tid)).fetchall()
            if {r[0] for r in lines} != set(got): raise err(422, "Enter a received quantity for every line, zero if nothing arrived")
            for lid, sid, bid, sent in lines:
                rc = got[lid]
                if rc < 0 or rc > sent: raise err(422, "Received quantity cannot be negative or above what was sent")
                post(c, NOW, t["to_location_id"], sid, bid, sent, "transfer_in", "transfer", t["transfer_no"], u["id"])
                if rc < sent: post(c, NOW, t["to_location_id"], sid, bid, rc - sent, "transit_shrink", "transfer", t["transfer_no"], u["id"])
                c.execute(text("update transfer_line set qty_received=:r where id=:i"), dict(r=rc, i=lid))
            c.execute(text("update transfer set status='received', received_at=:n where id=:i"), dict(n=NOW, i=tid))
            if t["indent_id"]: c.execute(text("update indent set status='received' where id=:i"), dict(i=t["indent_id"]))
            audit(c, u, "transfer_receive", "transfer", tid, dict(status="in_transit"), dict(status="received", short=sum(1 for r in lines if got[r[0]] < r[3])))
        return transfer_detail(tid, u)

    @app.post("/api/inv/transfers/{tid}/cancel")
    def transfer_cancel(tid: int, u=Depends(inv_read)):
        t = tr_get(tid, u); mover(u, t["from_location_id"])
        if t["status"] != "draft": raise err(422, "Only draft transfers can be cancelled. Dispatched stock is received, with short quantities where needed")
        with engine.begin() as c:
            c.execute(text("update transfer set status='cancelled' where id=:i"), dict(i=tid)); audit(c, u, "transfer_cancel", "transfer", tid, dict(status="draft"), dict(status="cancelled"))
        return transfer_detail(tid, u)

    # ---------------------------------------------------------------- indents
    @app.get("/api/inv/indents")
    def indents(status: str = "", page: int = 1, size: int = 30, u=Depends(inv_read)):
        a = allowed(u); w, p = [], {}
        if status: w.append("i.status=:st"); p["st"] = status
        if a is not None: w.append(f"(i.store_id in {inlist(a)} or i.warehouse_id in {inlist(a)})")
        wh = (" where " + " and ".join(w)) if w else ""
        total = one("select count(*) n from indent i" + wh, **p)["n"]
        items = many("select i.id, i.indent_no, i.status, i.created_at, i.needed_by, st.code store_code, st.name store_name, wh.code wh_code, (select count(*) from indent_line il where il.indent_id=i.id) lines, "
                     "(select sum(il.qty_req) from indent_line il where il.indent_id=i.id) req, (select sum(il.qty_alloc) from indent_line il where il.indent_id=i.id) alloc "
                     "from indent i join location st on st.id=i.store_id join location wh on wh.id=i.warehouse_id" + wh + " order by i.created_at desc limit :lim offset :off", **p, lim=size, off=(page - 1) * size)
        return dict(total=total, page=page, size=size, items=items)

    def ind_get(iid, u):
        i = one("select i.*, st.code store_code, st.name store_name, wh.code wh_code, wh.name wh_name from indent i join location st on st.id=i.store_id join location wh on wh.id=i.warehouse_id where i.id=:i", i=iid)
        if not i: raise err(404, "Indent not found")
        a = allowed(u)
        if a is not None and i["store_id"] not in a and i["warehouse_id"] not in a: raise err(403, "That indent is outside your scope")
        return i

    @app.get("/api/inv/indents/{iid}")
    def indent_detail(iid: int, u=Depends(inv_read)):
        i = ind_get(iid, u)
        i["lines"] = many("select il.id, il.sku_id, s.name, s.code, s.base_uom, il.qty_req, il.qty_alloc, il.qty_dispatched, (select group_concat(bt.batch_no || ' x ' || p.qty, ', ') from pick p join batch bt on bt.id=p.batch_id where p.indent_line_id=il.id) picks "
                          "from indent_line il join sku s on s.id=il.sku_id where il.indent_id=:i order by il.id", i=iid)
        i["transfer"] = one("select id, transfer_no, status from transfer where indent_id=:i", i=iid)
        return i

    @app.post("/api/inv/indents")
    def indent_create(b: IndIn, u=Depends(inv_read)):
        if u["role"] == "store_manager": sid_ = u["scope_id"]
        elif u["role"] in WRITE: sid_ = b.store_id
        else: raise err(403, "Your role cannot raise indents")
        if not sid_: raise err(422, "Pick the store this indent is for")
        st = one("select id, serving_wh_id, type from location where id=:i", i=sid_)
        if not st or st["type"] != "store": raise err(422, "Indents are raised by stores")
        see(u, sid_)
        need_by = b.needed_by or str(NOW.date() + timedelta(days=3))
        if need_by < TODAY or need_by > str(NOW.date() + timedelta(days=30)): raise err(422, "Needed by must be between today and 30 days out")
        if not b.lines: raise err(422, "Add at least one line")
        want = merge(b.lines); sk = sku_map(list(want))
        ass = {r["sku_id"]: r["max_qty"] for r in many(f"select sku_id, max_qty from store_assortment where store_id=:s and sku_id in {inlist(want)}", s=sid_)}
        for sid, qty in want.items():
            if sid not in sk: raise err(422, f"Unknown SKU {sid}")
            check_qty(sk[sid], qty)
            if sk[sid]["status"] != "active": raise err(422, f"{sk[sid]['name']} is {sk[sid]['status']} and cannot be indented")
            if sid not in ass: raise err(422, f"{sk[sid]['name']} is not in this store's assortment")
            if qty > ass[sid] * 2: raise err(422, f"{sk[sid]['name']}: {qty} is far above this store's max level of {ass[sid]}")
        with engine.begin() as c:
            n = c.execute(text("select coalesce(max(id),0)+1 from indent")).scalar(); no = f"IN-26{n:04d}"
            c.execute(text("insert into indent(indent_no, store_id, warehouse_id, status, created_at, needed_by) values (:n,:s,:w,'open',:c,:b)"), dict(n=no, s=sid_, w=st["serving_wh_id"], c=NOW, b=need_by))
            iid = c.execute(text("select id from indent where indent_no=:n"), dict(n=no)).scalar()
            for sid, qty in want.items(): c.execute(text("insert into indent_line(indent_id, sku_id, qty_req, qty_alloc, qty_dispatched) values (:i,:s,:q,0,0)"), dict(i=iid, s=sid, q=qty))
            audit(c, u, "indent_create", "indent", iid, None, dict(store_id=sid_, lines=len(want)))
        return indent_detail(iid, u)

    @app.post("/api/inv/indents/{iid}/allocate")
    def indent_allocate(iid: int, u=Depends(inv_read)):
        i = ind_get(iid, u); mover(u, i["warehouse_id"])
        if i["status"] not in ("open", "allocated"): raise err(422, f"Cannot allocate an indent that is {i['status']}")
        with engine.begin() as c:
            c.execute(text("delete from pick where indent_line_id in (select id from indent_line where indent_id=:i)"), dict(i=iid)); total = 0
            for lid, sid, req in c.execute(text("select id, sku_id, qty_req from indent_line where indent_id=:i"), dict(i=iid)).fetchall():
                got, _ = fefo(c, i["warehouse_id"], sid, req, skip_indent=iid); n = sum(q for _, q in got); total += n
                for bid, q in got: c.execute(text("insert into pick(indent_line_id, batch_id, qty) values (:l,:b,:q)"), dict(l=lid, b=bid, q=q))
                c.execute(text("update indent_line set qty_alloc=:a where id=:i"), dict(a=n, i=lid))
            if total == 0: raise err(422, "Nothing is in stock to allocate for this indent")
            c.execute(text("update indent set status='allocated' where id=:i"), dict(i=iid)); audit(c, u, "indent_allocate", "indent", iid, dict(status=i["status"]), dict(status="allocated", qty=total))
        return indent_detail(iid, u)

    @app.post("/api/inv/indents/{iid}/dispatch")
    def indent_dispatch(iid: int, b: DispIn, u=Depends(inv_read)):
        i = ind_get(iid, u); mover(u, i["warehouse_id"])
        if i["status"] != "allocated": raise err(422, "Allocate the indent before dispatching")
        with engine.begin() as c:
            picks = [(r[0], r[1], r[2]) for r in c.execute(text("select p.batch_id, il.sku_id, sum(p.qty) from pick p join indent_line il on il.id=p.indent_line_id where il.indent_id=:i group by p.batch_id, il.sku_id order by il.sku_id"), dict(i=iid)).fetchall()]
            for bid, sid, qty in picks:
                have = c.execute(text("select qty from stock_balance where location_id=:l and sku_id=:s and batch_id=:b"), dict(l=i["warehouse_id"], s=sid, b=bid)).scalar() or 0
                if have < qty: raise err(422, "Stock changed since allocation. Allocate again")
            n = c.execute(text("select coalesce(max(id),0)+1 from transfer")).scalar(); no = f"TR-26{n:04d}"
            c.execute(text("insert into transfer(transfer_no, indent_id, from_location_id, to_location_id, status, created_at) values (:n,:i,:f,:t,'draft',:c)"), dict(n=no, i=iid, f=i["warehouse_id"], t=i["store_id"], c=NOW))
            t = dict(id=c.execute(text("select id from transfer where transfer_no=:n"), dict(n=no)).scalar(), transfer_no=no, from_location_id=i["warehouse_id"], to_location_id=i["store_id"])
            run_dispatch(c, u, t, picks, b.vehicle_no, b.eway_ref)
            c.execute(text("update pick set picked_at=:n where indent_line_id in (select id from indent_line where indent_id=:i)"), dict(n=NOW, i=iid))
            c.execute(text("update indent_line set qty_dispatched=qty_alloc where indent_id=:i"), dict(i=iid)); c.execute(text("update indent set status='dispatched' where id=:i"), dict(i=iid))
            audit(c, u, "indent_dispatch", "indent", iid, dict(status="allocated"), dict(status="dispatched", transfer=no))
        return indent_detail(iid, u)

    @app.post("/api/inv/indents/{iid}/cancel")
    def indent_cancel(iid: int, u=Depends(inv_read)):
        i = ind_get(iid, u)
        if not (u["role"] in WRITE or (u["role"] == "store_manager" and i["store_id"] == u["scope_id"])): raise err(403, "Your role cannot cancel this indent")
        if i["status"] not in ("open", "allocated"): raise err(422, f"Cannot cancel an indent that is {i['status']}")
        with engine.begin() as c:
            c.execute(text("delete from pick where indent_line_id in (select id from indent_line where indent_id=:i)"), dict(i=iid)); c.execute(text("update indent_line set qty_alloc=0 where indent_id=:i"), dict(i=iid))
            c.execute(text("update indent set status='cancelled' where id=:i"), dict(i=iid)); audit(c, u, "indent_cancel", "indent", iid, dict(status=i["status"]), dict(status="cancelled"))
        return indent_detail(iid, u)

    # ---------------------------------------------------------------- repack
    @app.get("/api/inv/repack/options")
    def repack_options(warehouse_id: int, u=Depends(inv_read)):
        see(u, warehouse_id)
        rows = many("select s.id, s.name, s.code, sum(b.qty) qty from stock_balance b join sku s on s.id=b.sku_id where b.location_id=:w and s.procurement_type='bulk' and b.qty>0 group by s.id order by s.name", w=warehouse_id)
        for r in rows:
            r["packs"] = many("select id, name, pack_g from sku where bulk_sku_id=:i and status='active' order by pack_g", i=r["id"])
            with engine.connect() as c: r["batches"] = [dict(id=x[0], batch_no=x[3], qty=x[1], expiry_date=x[2]) for x in batches_at(c, warehouse_id, r["id"])]
        return [r for r in rows if r["batches"]]

    @app.get("/api/inv/repack")
    def repack_list(warehouse_id: int = 0, page: int = 1, size: int = 25, u=Depends(inv_read)):
        a = allowed(u); w, p = [], {}
        if warehouse_id: see(u, warehouse_id); w.append("j.warehouse_id=:w"); p["w"] = warehouse_id
        elif a is not None: w.append(f"j.warehouse_id in {inlist(a)}")
        wh = (" where " + " and ".join(w)) if w else ""
        total = one("select count(*) n from repack_job j" + wh, **p)["n"]
        items = many("select j.id, j.job_no, j.status, j.started_at, j.input_g, j.loss_g, w.code wh_code, s.name bulk_name, bt.batch_no, 1.0*j.loss_g/j.input_g loss_pct, "
                     "(select group_concat(o.units || ' x ' || ps.name, '; ') from repack_output o join sku ps on ps.id=o.sku_id where o.job_id=j.id) outputs from repack_job j join location w on w.id=j.warehouse_id join sku s on s.id=j.bulk_sku_id join batch bt on bt.id=j.input_batch_id"
                     + wh + " order by j.started_at desc limit :lim offset :off", **p, lim=size, off=(page - 1) * size)
        return dict(total=total, page=page, size=size, items=items)

    @app.post("/api/inv/repack")
    def repack_create(b: RepIn, u=Depends(inv_read)):
        mover(u, b.warehouse_id); L = loc_row(b.warehouse_id)
        if L["type"] != "warehouse": raise err(422, "Repacking happens in a warehouse")
        bulk = one("select id, name, procurement_type, base_uom from sku where id=:i", i=b.bulk_sku_id)
        if not bulk or bulk["procurement_type"] != "bulk": raise err(422, "Pick a loose bulk SKU to repack")
        if b.input_g <= 0 or b.input_g % 100: raise err(422, "Input must be a positive multiple of 100 g")
        bt = one("select id, batch_no, sku_id, expiry_date, unit_cost_paise from batch where id=:i", i=b.batch_id)
        if not bt or bt["sku_id"] != b.bulk_sku_id: raise err(422, "That batch does not belong to this SKU")
        if bt["expiry_date"] < TODAY: raise err(422, "That batch has expired and cannot be repacked")
        if not b.outputs: raise err(422, "Add at least one pack output")
        packs = {r["id"]: r for r in many(f"select id, name, pack_g, bulk_sku_id, shelf_life_days, mrp_paise, status from sku where id in {inlist([o.sku_id for o in b.outputs])}")}
        packed = 0
        for o in b.outputs:
            p_ = packs.get(o.sku_id)
            if not p_ or p_["bulk_sku_id"] != b.bulk_sku_id: raise err(422, "Every output must be a pack of the same bulk SKU")
            if p_["status"] != "active": raise err(422, f"{p_['name']} is {p_['status']}")
            if o.units <= 0: raise err(422, "Units must be positive")
            packed += o.units * p_["pack_g"]
        if packed > b.input_g: raise err(422, f"Packs add up to {packed} g, more than the {b.input_g} g put in")
        loss = b.input_g - packed; pct = loss * 100.0 / b.input_g
        if pct > MAX_LOSS_PCT: raise err(422, f"Yield loss of {pct:.1f}% is above the {MAX_LOSS_PCT:.0f}% limit. Recount the input or the packs")
        with engine.begin() as c:
            if not any(x[0] == b.batch_id and x[1] >= b.input_g for x in batches_at(c, b.warehouse_id, b.bulk_sku_id)): raise err(422, "Not enough unreserved stock in that batch")
            n = c.execute(text("select coalesce(max(id),0)+1 from repack_job")).scalar(); no = f"RP-26{n:04d}"
            c.execute(text("insert into repack_job(job_no, warehouse_id, bulk_sku_id, input_batch_id, input_g, status, started_at, finished_at, operator_id, loss_g) values (:n,:w,:s,:b,:g,'done',:t,:t,:u,:l)"), dict(n=no, w=b.warehouse_id, s=b.bulk_sku_id, b=b.batch_id, g=b.input_g, t=NOW, u=u["id"], l=loss))
            jid = c.execute(text("select id from repack_job where job_no=:n"), dict(n=no)).scalar()
            post(c, NOW, b.warehouse_id, b.bulk_sku_id, b.batch_id, -b.input_g, "repack_out", "repack", no, u["id"])
            for o in b.outputs:
                p_ = packs[o.sku_id]; exp = min(bt["expiry_date"], str(NOW.date() + timedelta(days=p_["shelf_life_days"]))); cost = int(bt["unit_cost_paise"] / 1000.0 * p_["pack_g"] * b.input_g / packed)
                bn = f"{bt['batch_no']}-R{jid}-{o.sku_id}"
                c.execute(text("insert into batch(batch_no, sku_id, vendor_id, received_on, mfg_date, expiry_date, mrp_paise, unit_cost_paise, source_doc_type, source_doc_id) values (:n,:s,null,:d,:d,:e,:m,:c,'repack',:j)"), dict(n=bn, s=o.sku_id, d=TODAY, e=exp, m=p_["mrp_paise"], c=cost, j=no))
                nb = c.execute(text("select id from batch where batch_no=:n"), dict(n=bn)).scalar()
                post(c, NOW, b.warehouse_id, o.sku_id, nb, o.units, "repack_in", "repack", no, u["id"])
                c.execute(text("insert into repack_output(job_id, sku_id, batch_id, units) values (:j,:s,:b,:u)"), dict(j=jid, s=o.sku_id, b=nb, u=o.units))
            audit(c, u, "repack", "repack_job", jid, None, dict(input_g=b.input_g, loss_g=loss, loss_pct=round(pct, 2)))
        return dict(id=jid, job_no=no, input_g=b.input_g, packed_g=packed, loss_g=loss, loss_pct=round(pct, 2), warn=pct > 2.0)

    # ---------------------------------------------------------------- cycle counts
    @app.get("/api/inv/counts")
    def counts(location_id: int = 0, status: str = "", u=Depends(inv_read)):
        a = allowed(u); w, p = [], {}
        if location_id: see(u, location_id); w.append("cp.location_id=:l"); p["l"] = location_id
        elif a is not None: w.append(f"cp.location_id in {inlist(a)}")
        if status: w.append("cp.status=:s"); p["s"] = status
        return many("select cp.id, cp.plan_no, cp.abc_class, cp.planned_on, cp.status, l.code loc_code, l.name loc_name, (select count(*) from count_line cl where cl.plan_id=cp.id) lines, "
                    "(select count(*) from count_line cl where cl.plan_id=cp.id and cl.variance_qty<>0) with_variance from count_plan cp join location l on l.id=cp.location_id" + ((" where " + " and ".join(w)) if w else "") + " order by cp.planned_on desc, cp.id desc limit 80", **p)

    def plan_get(pid, u):
        cp = one("select cp.*, l.code loc_code, l.name loc_name from count_plan cp join location l on l.id=cp.location_id where cp.id=:i", i=pid)
        if not cp: raise err(404, "Count plan not found")
        see(u, cp["location_id"]); return cp

    @app.get("/api/inv/counts/{pid}")
    def count_detail(pid: int, u=Depends(inv_read)):
        cp = plan_get(pid, u)
        cp["lines"] = many("select cl.id, cl.sku_id, s.name, s.code, s.base_uom, bt.batch_no, bt.expiry_date, cl.system_qty, cl.counted_qty, cl.variance_qty, "
                           f"{val('abs(coalesce(cl.variance_qty,0))', 'bt.unit_cost_paise')} var_value from count_line cl join sku s on s.id=cl.sku_id join batch bt on bt.id=cl.batch_id where cl.plan_id=:i order by var_value desc, cl.id", i=pid)
        cp["approve_note"] = f"Variance above ₹{LINE_LIMIT // 100:,} on a line or ₹{PLAN_LIMIT // 100:,} in total needs CEO or admin approval"
        return cp

    @app.post("/api/inv/counts")
    def count_create(b: CountIn, u=Depends(inv_read)):
        mover(u, b.location_id); L = loc_row(b.location_id); cls = b.abc_class.upper()
        if cls not in ("A", "B", "C"): raise err(422, "Class must be A, B or C")
        if not 1 <= b.size <= 50: raise err(422, "Size must be between 1 and 50 lines")
        if one("select id from count_plan where location_id=:l and abc_class=:c and status in ('planned','submitted')", l=b.location_id, c=cls): raise err(422, f"A class {cls} count is already open for {L['name']}")
        rows = many(f"select b.sku_id, b.batch_id, b.qty, {val('b.qty', 'bt.unit_cost_paise')} v from stock_balance b join sku s on s.id=b.sku_id join batch bt on bt.id=b.batch_id where b.location_id=:l and b.qty>0 order by v desc, b.sku_id", l=b.location_id)
        if not rows: raise err(422, "No stock at this location to count")
        n = len(rows); ca, cb = max(1, -(-n * 2 // 10)), max(1, -(-n * 5 // 10)); pool = rows[:ca] if cls == "A" else rows[ca:cb] if cls == "B" else rows[cb:]
        if not pool: raise err(422, f"No class {cls} stock at this location")
        pick = pool[:b.size] if cls == "A" else pool[::max(1, len(pool) // b.size)][:b.size]
        with engine.begin() as c:
            n_ = c.execute(text("select coalesce(max(id),0)+1 from count_plan")).scalar(); no = f"CP-26{n_:04d}"
            c.execute(text("insert into count_plan(plan_no, location_id, abc_class, planned_on, status) values (:n,:l,:c,:d,'planned')"), dict(n=no, l=b.location_id, c=cls, d=TODAY))
            pid = c.execute(text("select id from count_plan where plan_no=:n"), dict(n=no)).scalar()
            for r in pick: c.execute(text("insert into count_line(plan_id, sku_id, batch_id, system_qty) values (:p,:s,:b,:q)"), dict(p=pid, s=r["sku_id"], b=r["batch_id"], q=r["qty"]))
            audit(c, u, "count_create", "count_plan", pid, None, dict(location_id=b.location_id, abc_class=cls, lines=len(pick)))
        return count_detail(pid, u)

    @app.post("/api/inv/counts/{pid}/submit")
    def count_submit(pid: int, b: CountSub, u=Depends(inv_read)):
        cp = plan_get(pid, u); mover(u, cp["location_id"])
        if cp["status"] != "planned": raise err(422, f"Only planned counts can be submitted, this one is {cp['status']}")
        got = {x.line_id: x.counted_qty for x in b.lines}
        with engine.begin() as c:
            lines = c.execute(text("select cl.id, cl.sku_id, cl.batch_id, s.base_uom from count_line cl join sku s on s.id=cl.sku_id where cl.plan_id=:p"), dict(p=pid)).fetchall()
            if {r[0] for r in lines} != set(got): raise err(422, "Enter a counted quantity for every line")
            for lid, sid, bid, uom in lines:
                q_ = got[lid]
                if q_ < 0: raise err(422, "Counted quantity cannot be negative")
                if uom == "g" and q_ % 100: raise err(422, "Loose stock is counted in multiples of 100 g")
                cur = c.execute(text("select qty from stock_balance where location_id=:l and sku_id=:s and batch_id=:b"), dict(l=cp["location_id"], s=sid, b=bid)).scalar() or 0
                c.execute(text("update count_line set system_qty=:s, counted_qty=:q, variance_qty=:v where id=:i"), dict(s=cur, q=q_, v=q_ - cur, i=lid))
            c.execute(text("update count_plan set status='submitted' where id=:i"), dict(i=pid)); audit(c, u, "count_submit", "count_plan", pid, dict(status="planned"), dict(status="submitted"))
        return count_detail(pid, u)

    @app.post("/api/inv/counts/{pid}/approve")
    def count_approve(pid: int, u=Depends(inv_read)):
        cp = plan_get(pid, u); mover(u, cp["location_id"])
        if cp["status"] != "submitted": raise err(422, f"Only submitted counts can be approved, this one is {cp['status']}")
        d = count_detail(pid, u); big = u["role"] not in ("ceo", "admin")
        tot = sum(int(l["var_value"] or 0) for l in d["lines"])
        if big and (any((l["var_value"] or 0) > LINE_LIMIT for l in d["lines"]) or tot > PLAN_LIMIT): raise err(403, d["approve_note"])
        with engine.begin() as c:
            for l in d["lines"]:
                v = l["variance_qty"] or 0
                if v: post(c, NOW, cp["location_id"], l["sku_id"], c.execute(text("select batch_id from count_line where id=:i"), dict(i=l["id"])).scalar(), v, "count_adjust", "count_plan", cp["plan_no"], u["id"])
                c.execute(text("update count_line set approved_by=:u where id=:i"), dict(u=u["id"], i=l["id"]))
            c.execute(text("update count_plan set status='closed' where id=:i"), dict(i=pid)); audit(c, u, "count_approve", "count_plan", pid, dict(status="submitted"), dict(status="closed", variance_value=tot))
        return count_detail(pid, u)

    # ---------------------------------------------------------------- gift kit readiness
    def kit_rows():
        kits = many("select s.id, s.name from sku s where s.procurement_type='kit' and s.status='active'")
        bomr = many("select k.kit_sku_id, k.component_sku_id, k.qty, c.name from kit_bom k join sku c on c.id=k.component_sku_id")
        need = {r["id"]: r["n"] for r in many("select sku_id id, sum(max_qty) n from store_assortment where sku_id in (select id from sku where procurement_type='kit') group by 1")}
        have = {r["id"]: r["n"] for r in many("select sku_id id, sum(qty) n from stock_balance where sku_id in (select id from sku where procurement_type='kit') group by 1")}
        comp = {r["id"]: r["n"] for r in many("select b.sku_id id, sum(b.qty) n from stock_balance b join location l on l.id=b.location_id and l.type='warehouse' where b.sku_id in (select component_sku_id from kit_bom) group by 1")}
        out = []
        for k in kits:
            parts = [r for r in bomr if r["kit_sku_id"] == k["id"]]
            if not parts: continue
            lim = min(parts, key=lambda r: (comp.get(r["component_sku_id"], 0) // r["qty"], r["component_sku_id"])); bld = comp.get(lim["component_sku_id"], 0) // lim["qty"]
            gap = max(0, (need.get(k["id"]) or 0) * FEST_FACTOR - (have.get(k["id"]) or 0))
            out.append(dict(id=k["id"], name=k["name"], need=(need.get(k["id"]) or 0) * FEST_FACTOR, on_hand=have.get(k["id"]) or 0, gap=gap, buildable=bld, short=bld < gap, shortfall=max(0, gap - bld),
                            limiter=dict(id=lim["component_sku_id"], name=lim["name"], per_kit=lim["qty"], on_hand=comp.get(lim["component_sku_id"], 0))))
        return sorted(out, key=lambda r: (-r["short"], -r["shortfall"], r["name"]))

    @app.get("/api/inv/kits")
    def kits(u=Depends(inv_over)):
        rows = kit_rows(); return dict(assumption=f"Festival stocking assumption: stores need {FEST_FACTOR}x their usual max level of each gift box", short=sum(1 for r in rows if r["short"]), total=len(rows), items=rows[:60])

    @app.get("/api/inv/shrink")
    def shrink(warehouse_id: int, u=Depends(inv_over)):
        see(u, warehouse_id); lim = str((NOW - timedelta(days=90)).date())
        by_reason = many(f"select sl.doc_id reason, count(*) events, sum({val('-sl.qty', 'sl.unit_cost_paise')}) value from stock_ledger sl join sku s on s.id=sl.sku_id where sl.location_id=:w and sl.movement_type='shrink' and sl.ts>=:d group by 1 order by value desc", w=warehouse_id, d=lim)
        top = many(f"select s.name, sum({val('-sl.qty', 'sl.unit_cost_paise')}) value from stock_ledger sl join sku s on s.id=sl.sku_id where sl.location_id=:w and sl.movement_type='shrink' and sl.ts>=:d group by s.id order by value desc limit 8", w=warehouse_id, d=lim)
        return dict(by_reason=by_reason, top_skus=top)
