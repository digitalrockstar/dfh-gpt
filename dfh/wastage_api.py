"""Module 5 API: wastage and shrink. Attached to the app by dfh.app.create_app."""
import json, statistics
from datetime import timedelta
from typing import Optional
from fastapi import Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from .sql_dialect import days_between, date_plus_days, date_plus_literal_days
from . import stockpost

VIEW = {"ceo", "finance", "admin", "procurement", "warehouse", "store_manager"}
ENTER = {"ceo", "admin", "warehouse", "store_manager"}
APPROVE = {"ceo", "finance", "admin", "warehouse"}
MARK = {"ceo", "procurement", "admin", "store_manager"}
APPROVAL_LIMIT = 200000   # paise. Entries at or above Rs 2,000 wait for approval.
COST = "case when s.base_uom='g' then {q}/1000.0*s.std_cost_paise else {q}*s.std_cost_paise end"


class EntryIn(BaseModel):
    location_id: int
    sku_id: int
    qty: int
    reason: str
    batch_id: Optional[int] = None
    note: Optional[str] = None


class Decision(BaseModel):
    action: str


class MarkIn(BaseModel):
    action: str


def money(p):
    r = p / 100
    return f"₹{r / 1e7:.2f} Cr" if r >= 1e7 else f"₹{r / 1e5:.1f} L" if r >= 1e5 else f"₹{r:,.0f}"


def attach(app, ctx):
    me, need, many, one, audit, engine, an, NOW = (ctx[k] for k in ("me", "need", "many", "one", "audit", "engine", "an", "NOW"))
    view = need(VIEW)

    def loc_scope(u):
        if u["scope_type"] == "store": return [u["scope_id"]]
        if u["scope_type"] == "warehouse":
            return [u["scope_id"]] + [r["id"] for r in many("select id from location where serving_wh_id=:w", w=u["scope_id"])]
        return None

    def where(u, alias="l", region="", city="", tier=""):
        w, p = [], {}
        sc = loc_scope(u)
        if sc is not None: w.append(f"{alias}.id in ({','.join(str(int(x)) for x in sc) or '-1'})")
        if region: w.append(f"{alias}.region=:rg"); p["rg"] = region
        if city: w.append(f"{alias}.city=:ct"); p["ct"] = city
        if tier: w.append(f"{alias}.tier=:tr"); p["tr"] = tier
        return ("".join(" and " + x for x in w)), p

    def span(days):
        s, e, ps, pe, has = an.period(days)
        return dict(s=str(s), e=str(e + timedelta(days=1)), ps=str(ps), pe=str(pe + timedelta(days=1))), has, s, e

    def store_cogs(s, e, ids=None):
        rows = an.q("select store_id, sum(cogs) from agg where date between ? and ? group by 1", [s, e])
        return {r[0]: float(r[1]) for r in rows if ids is None or r[0] in ids}

    @app.get("/api/wastage/summary")
    def summary(days: int = 30, region: str = "", city: str = "", tier: str = "", u=Depends(view)):
        sp, has, s, e = span(days); w, p = where(u, "l", region, city, tier)
        base = ("select coalesce(sum(x.cost_paise),0) cost, count(*) n, coalesce(sum(case when r.is_controllable=1 then x.cost_paise else 0 end),0) ctl from wastage_entry x "
                "join location l on l.id=x.location_id join wastage_reason r on r.code=x.reason where x.status!='rejected' and x.ts>=:a and x.ts<:b" + w)
        cur = one(base, a=sp["s"], b=sp["e"], **p); prev = one(base, a=sp["ps"], b=sp["pe"], **p) if has else None
        st = one("select coalesce(sum(x.cost_paise),0) cost from wastage_entry x join location l on l.id=x.location_id where l.type='store' and x.status!='rejected' and x.ts>=:a and x.ts<:b" + w, a=sp["s"], b=sp["e"], **p)
        ids = [r["id"] for r in many("select l.id from location l where l.type='store'" + w, **p)]
        cogs = sum(store_cogs(s, e, set(ids)).values())
        top = one("select x.reason, r.name, sum(x.cost_paise) c from wastage_entry x join location l on l.id=x.location_id join wastage_reason r on r.code=x.reason where x.status!='rejected' and x.ts>=:a and x.ts<:b" + w + " group by 1,2 order by 3 desc limit 1", a=sp["s"], b=sp["e"], **p)
        pend = one("select count(*) n, coalesce(sum(x.cost_paise),0) c from wastage_entry x join location l on l.id=x.location_id where x.status='pending'" + w, **p)
        risk = expiry_value(u, 30, region, city, tier)
        d = lambda a, b: (a / b - 1) if b else None
        return dict(days=days, end=str(e), cost=int(cur["cost"]), entries=cur["n"], delta=d(cur["cost"], prev["cost"]) if prev else None, controllable=(cur["ctl"] / cur["cost"]) if cur["cost"] else 0,
                    store_cost=int(st["cost"]), store_pct_cogs=(st["cost"] / cogs) if cogs else None, top_reason=top, pending=pend, expiry_risk=risk)

    def expiry_value(u, window, region="", city="", tier=""):
        w, p = where(u, "l", region, city, tier)
        r = one("select coalesce(sum(" + COST.format(q="b.qty") + "),0) v, count(distinct b.sku_id) skus, count(*) lines from stock_balance b join batch t on t.id=b.batch_id join sku s on s.id=b.sku_id join location l on l.id=b.location_id "
                "where b.qty>0 and t.expiry_date <= {date_plus_days(engine, ":now", "win")}" + w, now=str(NOW.date()), win=window, **p)
        return dict(value=int(r["v"]), skus=r["skus"], lines=r["lines"], window=window)

    @app.get("/api/wastage/reasons")
    def reasons(days: int = 30, region: str = "", city: str = "", tier: str = "", u=Depends(view)):
        sp, has, s, e = span(days); w, p = where(u, "l", region, city, tier)
        q = ("select r.code, r.name, r.is_controllable ctl, r.cap_pct, coalesce(sum(case when x.ts>=:a and x.ts<:b then x.cost_paise end),0) cost, coalesce(sum(case when x.ts>=:a and x.ts<:b then 1 end),0) n, "
             "coalesce(sum(case when x.ts>=:pa and x.ts<:pb then x.cost_paise end),0) prev from wastage_reason r left join (select x.* from wastage_entry x join location l on l.id=x.location_id where x.status!='rejected'" + w + ") x on x.reason=r.code group by r.code order by cost desc")
        rows = many(q, a=sp["s"], b=sp["e"], pa=sp["ps"], pb=sp["pe"], **p); tot = sum(r["cost"] for r in rows) or 1
        for r in rows: r["share"] = r["cost"] / tot; r["delta"] = (r["cost"] / r["prev"] - 1) if has and r["prev"] else None
        return rows

    @app.get("/api/wastage/trend")
    def trend(days: int = 90, region: str = "", city: str = "", tier: str = "", u=Depends(view)):
        sp, has, s, e = span(days); w, p = where(u, "l", region, city, tier)
        rows = many("select substr(x.ts,1,10) d, sum(x.cost_paise) c, sum(case when r.is_controllable=1 then x.cost_paise else 0 end) k from wastage_entry x join location l on l.id=x.location_id join wastage_reason r on r.code=x.reason "
                    "where x.status!='rejected' and x.ts>=:a and x.ts<:b" + w + " group by 1 order by 1", a=sp["s"], b=sp["e"], **p)
        if days <= 60: return [dict(date=r["d"], cost=int(r["c"]), controllable=int(r["k"])) for r in rows]
        out = {}
        for r in rows:
            from datetime import date as D
            d = D.fromisoformat(r["d"]); k = str(d - timedelta(days=d.weekday()))
            o = out.setdefault(k, dict(date=k, cost=0, controllable=0)); o["cost"] += int(r["c"]); o["controllable"] += int(r["k"])
        return list(out.values())

    @app.get("/api/wastage/locations")
    def locations(days: int = 30, kind: str = "store", order: str = "worst", region: str = "", city: str = "", tier: str = "", u=Depends(view)):
        sp, has, s, e = span(days); w, p = where(u, "l", region, city, tier)
        rows = many("select l.id, l.code, l.name, l.city, l.tier, coalesce(sum(x.cost_paise),0) cost, count(x.id) n, "
                    "coalesce(sum(case when x.reason='THEFT' then x.cost_paise end),0) theft, coalesce(sum(case when x.reason in ('WEIGHT','SAMPLE') then x.cost_paise end),0) wt, "
                    "coalesce(sum(case when x.reason='EXPIRY' then x.cost_paise end),0) expiry from location l left join wastage_entry x on x.location_id=l.id and x.status!='rejected' and x.ts>=:a and x.ts<:b "
                    "where l.type=:k" + w + " group by l.id", a=sp["s"], b=sp["e"], k=kind, **p)
        if kind == "store":
            cg = store_cogs(s, e)
            for r in rows: r["pct"] = (r["cost"] / cg[r["id"]]) if cg.get(r["id"]) else None
            rows = [r for r in rows if r["pct"] is not None]; metric = "pct"
        else:
            for r in rows: r["pct"] = None
            metric = "cost"
        med = statistics.median([r[metric] for r in rows]) if rows else 0
        for r in rows:
            r["vs_peer"] = (r[metric] / med) if med else None
            r["flag"] = bool(r["vs_peer"] and r["vs_peer"] >= 2.0 and r["cost"] >= 100000)
            parts = dict(Theft=r["theft"], Weighing=r["wt"], Expiry=r["expiry"]); k = max(parts, key=parts.get); r["lead"] = k if parts[k] > 0 else None
            r["name"] = r["name"].replace("Dry Fruit House ", "")
        rows.sort(key=lambda r: r[metric], reverse=(order != "best"))
        return dict(kind=kind, median=med, total=len(rows), rows=rows[:15])

    @app.get("/api/wastage/skus")
    def skus(days: int = 30, reason: str = "", kind: str = "", region: str = "", city: str = "", tier: str = "", u=Depends(view)):
        sp, has, s, e = span(days); w, p = where(u, "l", region, city, tier)
        if reason: w += " and x.reason=:rs"; p["rs"] = reason
        if kind in ("store", "warehouse"): w += " and l.type=:kd"; p["kd"] = kind
        return many("select s.id, s.code, s.name, sum(x.cost_paise) cost, count(*) n, coalesce(sum(case when x.reason='EXPIRY' then x.cost_paise end),0) expiry from wastage_entry x join sku s on s.id=x.sku_id join location l on l.id=x.location_id "
                    "where x.status!='rejected' and x.ts>=:a and x.ts<:b" + w + " group by s.id order by cost desc limit 10", a=sp["s"], b=sp["e"], **p)

    @app.get("/api/wastage/expiry")
    def expiry(window: int = 30, region: str = "", city: str = "", tier: str = "", kind: str = "", u=Depends(view)):
        window = window if window in (15, 30, 60) else 30; w, p = where(u, "l", region, city, tier)
        if kind in ("store", "warehouse"): w += " and l.type=:kd"; p["kd"] = kind
        base = ("from stock_balance b join batch t on t.id=b.batch_id join sku s on s.id=b.sku_id join location l on l.id=b.location_id left join markdown_suggestion m on m.location_id=b.location_id and m.batch_id=b.batch_id "
                "where b.qty>0 and t.expiry_date <= {date_plus_literal_days(engine, ":now", 60)}" + w)
        val = COST.format(q="b.qty")
        rows = many(f"select b.location_id, b.batch_id, b.qty, l.code lcode, l.name lname, l.city, l.type ltype, s.id sku_id, s.name sku, s.base_uom, t.batch_no, t.expiry_date, "
                    f"{days_between(engine, "t.expiry_date", ":now")} days_left, cast({val} as integer) value, m.id mid, m.suggested_pct, m.status mstatus {base}", now=str(NOW.date()), **p)
        buckets = dict(expired=dict(n=0, value=0), d15=dict(n=0, value=0), d30=dict(n=0, value=0), d60=dict(n=0, value=0))
        for r in rows:
            k = "expired" if r["days_left"] < 0 else "d15" if r["days_left"] <= 15 else "d30" if r["days_left"] <= 30 else "d60"
            buckets[k]["n"] += 1; buckets[k]["value"] += r["value"]
        keep = sorted((r for r in rows if r["days_left"] <= window), key=lambda r: -r["value"])
        for r in keep: r["lname"] = r["lname"].replace("Dry Fruit House ", "")
        by_sku = {}
        for r in keep:
            o = by_sku.setdefault(r["sku_id"], dict(sku=r["sku"], value=0, locations=set())); o["value"] += r["value"]; o["locations"].add(r["location_id"])
        top = sorted(({"sku": o["sku"], "value": o["value"], "locations": len(o["locations"])} for o in by_sku.values()), key=lambda x: -x["value"])[:5]
        return dict(window=window, buckets=buckets, total=len(keep), value=sum(r["value"] for r in keep), top_skus=top, rows=keep[:40])

    @app.get("/api/wastage/entries")
    def entries(status: str = "", reason: str = "", location_id: int = 0, page: int = 1, size: int = 25, u=Depends(view)):
        w, p = where(u, "l")
        if status: w += " and x.status=:st"; p["st"] = status
        if reason: w += " and x.reason=:rs"; p["rs"] = reason
        if location_id: w += " and x.location_id=:li"; p["li"] = location_id
        size = max(1, min(size, 100))
        total = one("select count(*) n from wastage_entry x join location l on l.id=x.location_id where 1=1" + w, **p)["n"]
        rows = many("select x.id, x.ts, x.qty, x.reason, r.name reason_name, x.cost_paise, x.status, x.photo_ref, x.vendor_claim, l.name lname, l.type ltype, s.name sku, s.base_uom, a.name approver, e.name by_user from wastage_entry x "
                    "join location l on l.id=x.location_id join sku s on s.id=x.sku_id join wastage_reason r on r.code=x.reason left join app_user a on a.id=x.approved_by left join app_user e on e.id=x.entered_by where 1=1" + w +
                    " order by (x.status='pending') desc, x.ts desc limit :n offset :o", n=size, o=(max(1, page) - 1) * size, **p)
        for r in rows: r["lname"] = r["lname"].replace("Dry Fruit House ", ""); r["ts"] = str(r["ts"])[:16]
        return dict(total=total, page=page, size=size, items=rows)

    @app.get("/api/wastage/options")
    def options(u=Depends(view)):
        w, p = where(u, "l")
        return dict(reasons=many("select code, name, is_controllable from wastage_reason order by id"), locations=many("select l.id, l.name, l.type, l.city from location l where l.type in ('store','warehouse')" + w + " order by l.type desc, l.name", **p),
                    can_enter=u["role"] in ENTER, can_approve=u["role"] in APPROVE, can_mark=u["role"] in MARK, approval_limit=APPROVAL_LIMIT)

    @app.post("/api/wastage/entries")
    def create_entry(b: EntryIn, u=Depends(need(ENTER))):
        sc = loc_scope(u)
        if sc is not None and b.location_id not in sc: raise HTTPException(403, "That location is outside your scope")
        loc = one("select id, type from location where id=:i and type in ('store','warehouse')", i=b.location_id)
        if not loc: raise HTTPException(422, "Pick a store or warehouse")
        sku = one("select id, name, base_uom, std_cost_paise from sku where id=:i", i=b.sku_id)
        if not sku: raise HTTPException(422, "SKU not found")
        if not one("select code from wastage_reason where code=:c", c=b.reason): raise HTTPException(422, "Pick a reason from the list")
        if b.qty <= 0: raise HTTPException(422, "Quantity must be more than zero")
        unit = "g" if sku["base_uom"] == "g" else "units"
        if b.qty > (500000 if unit == "g" else 5000): raise HTTPException(422, f"That quantity looks wrong ({b.qty} {unit}). Check and try again")
        cost = round(b.qty / 1000 * sku["std_cost_paise"]) if unit == "g" else b.qty * sku["std_cost_paise"]
        status = "pending" if cost >= APPROVAL_LIMIT else "approved"
        with engine.begin() as c:
            if b.batch_id:
                bt = c.execute(text("select id from batch where id=:b and sku_id=:s"), dict(b=b.batch_id, s=b.sku_id)).fetchone()
                if not bt: raise HTTPException(422, "That batch does not belong to this SKU")
                bal = c.execute(text("select qty from stock_balance where location_id=:l and sku_id=:s and batch_id=:b"), dict(l=b.location_id, s=b.sku_id, b=b.batch_id)).fetchone()
                if not bal or bal[0] < b.qty: raise HTTPException(422, f"Only {bal[0] if bal else 0} {unit} of that batch is recorded at this location")
            r = c.execute(text("insert into wastage_entry(ts, location_id, sku_id, batch_id, qty, reason, cost_paise, entered_by, status, approved_by, photo_ref, vendor_claim) values (:t,:l,:s,:b,:q,:r,:c,:u,:st,:ap,null,0) returning id"),
                          dict(t=NOW, l=b.location_id, s=b.sku_id, b=b.batch_id, q=b.qty, r=b.reason, c=cost, u=u["id"], st=status, ap=u["id"] if status == "approved" and u["role"] in APPROVE else None))
            eid = r.scalar_one()
            if b.batch_id: stockpost.post(c, NOW, b.location_id, b.sku_id, b.batch_id, -b.qty, "wastage", "wastage_entry", eid, u["id"])
            audit(c, u, "create", "wastage_entry", eid, None, dict(location_id=b.location_id, sku_id=b.sku_id, qty=b.qty, reason=b.reason, cost_paise=cost, status=status, note=b.note))
        return dict(id=eid, cost_paise=cost, status=status)

    @app.post("/api/wastage/entries/{eid}/decision")
    def decide(eid: int, b: Decision, u=Depends(need(APPROVE))):
        if b.action not in ("approve", "reject"): raise HTTPException(422, "Action must be approve or reject")
        x = one("select id, location_id, sku_id, batch_id, qty, status, entered_by from wastage_entry where id=:i", i=eid)
        if not x: raise HTTPException(404, "Entry not found")
        sc = loc_scope(u)
        if sc is not None and x["location_id"] not in sc: raise HTTPException(403, "That location is outside your scope")
        if x["status"] != "pending": raise HTTPException(409, f"Entry is already {x['status']}")
        if x["entered_by"] == u["id"]: raise HTTPException(403, "You cannot approve your own entry")
        new = "approved" if b.action == "approve" else "rejected"
        with engine.begin() as c:
            c.execute(text("update wastage_entry set status=:s, approved_by=:u where id=:i"), dict(s=new, u=u["id"], i=eid))
            if new == "rejected" and x["batch_id"]:
                stockpost.post(c, NOW, x["location_id"], x["sku_id"], x["batch_id"], x["qty"], "wastage_reversal", "wastage_entry", eid, u["id"])
            audit(c, u, b.action, "wastage_entry", eid, dict(status="pending"), dict(status=new))
        return dict(id=eid, status=new)

    @app.post("/api/wastage/markdowns/{mid}")
    def mark(mid: int, b: MarkIn, u=Depends(need(MARK))):
        if b.action not in ("accept", "dismiss"): raise HTTPException(422, "Action must be accept or dismiss")
        m = one("select id, location_id, status from markdown_suggestion where id=:i", i=mid)
        if not m: raise HTTPException(404, "Suggestion not found")
        sc = loc_scope(u)
        if sc is not None and m["location_id"] not in sc: raise HTTPException(403, "That location is outside your scope")
        if m["status"] != "open": raise HTTPException(409, f"Suggestion is already {m['status']}")
        new = "accepted" if b.action == "accept" else "dismissed"
        with engine.begin() as c:
            c.execute(text("update markdown_suggestion set status=:s where id=:i"), dict(s=new, i=mid)); audit(c, u, b.action, "markdown_suggestion", mid, dict(status="open"), dict(status=new))
        return dict(id=mid, status=new)

    def exceptions(f):
        out = []; ids = f["store_ids"]
        if ids is not None: return out   # chain-level shrink and expiry exceptions are for head-office views
        a = str(an.max_date - timedelta(days=29)); b = str(an.max_date + timedelta(days=1))
        rows = many("select l.id, l.name, l.city, sum(x.cost_paise) c, sum(case when x.reason='THEFT' then x.cost_paise else 0 end) th, sum(case when x.reason='WEIGHT' then x.cost_paise else 0 end) wt from wastage_entry x join location l on l.id=x.location_id "
                    "where l.type='warehouse' and x.status!='rejected' and x.ts>=:a and x.ts<:b group by l.id", a=a, b=b)
        if rows:
            med = statistics.median(r["c"] for r in rows)
            for r in sorted(rows, key=lambda r: -r["c"])[:1]:
                if med and r["c"] >= 2 * med:
                    lead = "theft and weighing variance" if r["th"] + r["wt"] > r["c"] * 0.3 else "damage and spoilage"
                    out.append(dict(type="shrink", severity="high", title=f"{r['name']} shrink is {r['c'] / med:.1f}x the typical warehouse", detail=f"{r['city']}. Lost stock at cost over 30 days is {money(r['c'])} against a median of {money(med)}. {lead.capitalize()} lead the mix.", ref=r["id"]))
        e = one("select coalesce(sum(" + COST.format(q="b.qty") + "),0) v, count(distinct b.location_id) n from stock_balance b join batch t on t.id=b.batch_id join sku s on s.id=b.sku_id join location l on l.id=b.location_id where l.type='store' and b.qty>0 and t.expiry_date <= {date_plus_literal_days(engine, ":now", 30)}", now=str(NOW.date()))
        top = many("select s.name, sum(" + COST.format(q="b.qty") + ") v, count(distinct b.location_id) n from stock_balance b join batch t on t.id=b.batch_id join sku s on s.id=b.sku_id join location l on l.id=b.location_id where l.type='store' and b.qty>0 and t.expiry_date <= date(:now, '+30 day') group by s.name order by v desc limit 2", now=str(NOW.date()))
        if e["v"] and top:
            out.append(dict(type="expiry", severity="med", title=f"{money(e['v'])} of store stock is expired or expires within 30 days", detail=f"{e['n']} stores hold it. Largest: {', '.join(f'{t['name']} ({money(t['v'])}, {t['n']} stores)' for t in top)}. Markdown suggestions are open in Wastage.", ref="expiry"))
        return out

    return dict(exceptions=exceptions)
