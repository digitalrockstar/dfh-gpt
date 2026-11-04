"""Module 6 API: demand forecast, accuracy, buy suggestions, gift box components, store min and max. Attached by dfh.app.create_app."""
import json, math
from datetime import timedelta
from typing import Optional
from fastapi import Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text

VIEW = {"ceo", "finance", "admin", "procurement", "warehouse", "store_manager", "marketing"}
BUY_VIEW = {"ceo", "finance", "admin", "procurement", "warehouse"}
APPROVE = {"ceo", "finance", "admin", "procurement"}
BIG = {"ceo", "finance", "admin"}            # orders above the limit need one of these
PO_LIMIT = 250_000_000                       # paise, Rs 25 lakh
MM_ROLES = {"ceo", "procurement", "admin", "store_manager"}
PRICE = "coalesce(s.price_per_kg_paise / 1000.0, s.mrp_paise)"
URG = {"critical": 0, "soon": 1, "plan": 2}


class Decision(BaseModel):
    action: str
    qty: Optional[int] = None
    note: Optional[str] = None


class MinMaxIn(BaseModel):
    store_id: int
    sku_id: int


def attach(app, ctx):
    me, need, many, one, audit, engine, an, NOW = (ctx[k] for k in ("me", "need", "many", "one", "audit", "engine", "an", "NOW"))
    view = need(VIEW); buy_view = need(BUY_VIEW)

    def run():
        r = one("select id, run_at, horizon_weeks, wape, notes from forecast_run order by id desc limit 1")
        if not r: raise HTTPException(503, "Forecast has not been built. Run python -m dfh.demand.run")
        r["notes"] = json.loads(r["notes"]) if isinstance(r["notes"], (str, bytes, bytearray)) else r["notes"]; return r

    def stores(u):
        if u["scope_type"] == "store": return [u["scope_id"]]
        if u["scope_type"] == "warehouse": return [r["id"] for r in many("select id from location where serving_wh_id=:w", w=u["scope_id"])]
        return None

    def wh_scope(u):
        return [u["scope_id"]] if u["scope_type"] == "warehouse" else None

    def inlist(col, ids): return f"{col} in ({','.join(str(int(x)) for x in ids) or '-1'})"

    # ---------- forecast ----------
    def fc_series(u, scope, key):
        r = run(); n = r["notes"]; w, p = [], {}
        sc = stores(u)
        if sc is not None: w.append(inlist("f.store_id", sc))
        if scope == "category": w.append("c.code=:k"); p["k"] = key
        elif scope == "city": w.append("l.city=:k"); p["k"] = key
        elif scope == "store": w.append("f.store_id=:k"); p["k"] = int(key)
        elif scope == "sku": w.append("f.sku_id=:k"); p["k"] = int(key)
        elif scope != "chain": raise HTTPException(422, "Unknown scope")
        real = n["realisation"]
        rows = many(f"select week_start, sum(v) v, sum(sg * sg) var from (select f.week_start, f.value_paise v, (f.upper - f.lower) / 2.5632 * {PRICE} * {real} sg from forecast f "
                    "join sku s on s.id=f.sku_id join product pr on pr.id=s.product_id join category c on c.id=pr.category_id join location l on l.id=f.store_id where " + (" and ".join(w) or "1=1") + ") group by 1 order by 1", **p)
        rel = n["backtest"]["levels"]["chain_category_week" if scope in ("chain", "category") else "city_category_week"]["rel_rmse"] or .1
        out = []
        for x in rows:
            sd = math.sqrt(x["var"] + (rel * x["v"]) ** 2)
            out.append(dict(week=str(x["week_start"])[:10], value=int(x["v"]), lower=int(max(0, x["v"] - 1.2816 * sd)), upper=int(x["v"] + 1.2816 * sd)))
        return out

    def actual_series(u, scope, key, weeks=12):
        e = an.max_date; start = e - timedelta(days=7 * weeks); w, p = ["s.date > ?"], [start]
        sc = stores(u)
        if sc is not None: w.append(inlist("d.id", sc))
        if scope == "category": w.append("k.cat=?"); p.append(key)
        elif scope == "city": w.append("d.city=?"); p.append(key)
        elif scope == "store": w.append("d.id=?"); p.append(int(key))
        elif scope == "sku": w.append("s.sku_id=?"); p.append(int(key))
        rows = an.q("select (?::date - s.date) // 7 b, sum(s.net_paise) from sales s join dim_sku k on k.id=s.sku_id join dim_store d on d.id=s.store_id where " + " and ".join(w) + " group by 1", [e] + p)
        got = {int(b): int(v) for b, v in rows}
        return [dict(week=str(e - timedelta(days=7 * b + 6)), value=got.get(b, 0)) for b in range(weeks - 1, -1, -1)]

    @app.get("/api/demand/forecast")
    def forecast(scope: str = "chain", key: str = "", u=Depends(view)):
        if scope != "chain" and not key: raise HTTPException(422, "Pick a category, city, store or SKU")
        r = run()
        return dict(scope=scope, key=key, actual=actual_series(u, scope, key), forecast=fc_series(u, scope, key), festivals=[dict(name=f["name"], peak=str(f["peak_date"])[:10]) for f in many("select name, peak_date from festival_calendar order by peak_date")],
                    through=str(an.max_date), run_at=str(r["run_at"]))

    @app.get("/api/demand/accuracy")
    def accuracy(u=Depends(view)):
        r = run(); n = r["notes"]; cats = {c["code"]: c["name"] for c in many("select code, name from category")}
        return dict(run_at=str(r["run_at"]), method=n["backtest"], beta=[dict(code=k, name=cats[k], sensitivity=v) for k, v in n["beta"].items()], assumptions=n["assumptions"], band=n["band"], shrink_days=n["shrink_days"],
                    categories=[dict(code=k, name=cats[k], **v) for k, v in n["backtest"]["categories"].items()])

    @app.get("/api/demand/festivals")
    def festivals(u=Depends(view)):
        n = run()["notes"]
        return dict(calendar=many("select name, start_date, peak_date, ramp_days, uplift, regions from festival_calendar order by peak_date"), sensitivity=n["beta"])

    # ---------- buy suggestions ----------
    def sug_where(u, wh, cat, status, q, urgency=""):
        w, p = [], {}
        sc = wh_scope(u)
        if sc is not None: w.append(inlist("r.location_id", sc))
        if wh: w.append("r.location_id=:wh"); p["wh"] = wh
        if cat: w.append("c.code=:cat"); p["cat"] = cat
        if status: w.append("r.status=:st"); p["st"] = status
        if urgency: w.append("r.urgency=:ug"); p["ug"] = urgency
        if q: w.append("(lower(s.name) like :q or lower(s.code) like :q)"); p["q"] = f"%{q.lower()}%"
        return (" where " + " and ".join(w)) if w else "", p

    SUG = ("from reorder_suggestion r join sku s on s.id=r.sku_id join product pr on pr.id=s.product_id join category c on c.id=pr.category_id join location l on l.id=r.location_id left join vendor v on v.id=r.vendor_id")

    @app.get("/api/demand/options")
    def options(u=Depends(view)):
        buy = u["role"] in BUY_VIEW
        sc = wh_scope(u)
        return dict(warehouses=many("select id, name, city from location where type='warehouse'" + (f" and {inlist('id', sc)}" if sc else "") + " order by name"),
                    categories=many("select code, name from category order by id"), cities=[x["city"] for x in many("select distinct city from location where type='store' order by city")],
                    can_buy=buy, can_approve=u["role"] in APPROVE, can_minmax=u["role"] in MM_ROLES, po_limit=PO_LIMIT, minmax_roles=sorted(MM_ROLES))

    @app.get("/api/demand/suggestions")
    def suggestions(wh: int = 0, cat: str = "", urgency: str = "", status: str = "open", q: str = "", page: int = 1, size: int = 25, u=Depends(buy_view)):
        size = max(1, min(size, 100)); w, p = sug_where(u, wh, cat, status, q, urgency); w0, p0 = sug_where(u, wh, cat, status, q)
        counts = {x["urgency"]: dict(n=x["n"], value=int(x["v"])) for x in many("select r.urgency, count(*) n, sum(r.value_paise) v " + SUG + w0 + " group by 1", **p0)}
        tot = one("select count(*) n, coalesce(sum(r.value_paise), 0) v " + SUG + w, **p)
        items = many("select r.id, r.location_id, l.name wh_name, l.city, r.sku_id, s.code, s.name, s.base_uom, c.name cat, r.vendor_id, v.name vendor, v.country, r.suggested_qty, r.approved_qty, r.unit_cost_paise, r.value_paise, r.on_hand, r.on_order, "
                     "r.need_lead, r.need_cycle, r.cover_days, r.lead_time_days, r.moq, r.urgency, r.order_by, r.reason, r.status, r.decided_at " + SUG + w +
                     " order by case r.urgency when 'critical' then 0 when 'soon' then 1 else 2 end, r.value_paise desc, r.id limit :lim offset :off", **p, lim=size, off=(max(1, page) - 1) * size)
        return dict(total=tot["n"], value=int(tot["v"]), counts=counts, page=page, size=size, items=items)

    @app.post("/api/demand/suggestions/{sid}/decision")
    def decide(sid: int, b: Decision, u=Depends(need(APPROVE))):
        if b.action not in ("approve", "reject"): raise HTTPException(422, "Action must be approve or reject")
        x = one("select id, location_id, sku_id, suggested_qty, unit_cost_paise, moq, status from reorder_suggestion where id=:i", i=sid)
        if not x: raise HTTPException(404, "Suggestion not found")
        sc = wh_scope(u)
        if sc is not None and x["location_id"] not in sc: raise HTTPException(403, "That warehouse is outside your scope")
        if x["status"] != "open": raise HTTPException(409, f"Suggestion is already {x['status']}")
        qty = x["suggested_qty"]
        if b.action == "approve":
            if b.qty is not None:
                if b.qty <= 0: raise HTTPException(422, "Quantity must be positive")
                if b.qty < x["moq"]: raise HTTPException(422, f"The vendor minimum order is {x['moq']}")
                if b.qty > 3 * x["suggested_qty"]: raise HTTPException(422, "More than three times the suggestion. Check the quantity")
                qty = b.qty
            if qty * x["unit_cost_paise"] > PO_LIMIT and u["role"] not in BIG: raise HTTPException(403, "Orders above ₹25 lakh need the CEO, finance or admin to approve")
        new = "approved" if b.action == "approve" else "rejected"
        with engine.begin() as c:
            c.execute(text("update reorder_suggestion set status=:s, approved_qty=:q, decided_by=:u, decided_at=:t where id=:i"), dict(s=new, q=qty if b.action == "approve" else None, u=u["id"], t=NOW, i=sid))
            audit(c, u, b.action, "reorder_suggestion", sid, dict(status="open", qty=x["suggested_qty"]), dict(status=new, qty=qty if b.action == "approve" else None, note=b.note))
        return dict(id=sid, status=new, qty=qty if b.action == "approve" else None)

    # ---------- gift box components ----------
    def kit_rows(u):
        kits = many("select s.id, s.code, s.name, sum(f.qty_forecast) units from forecast f join sku s on s.id=f.sku_id where s.procurement_type='kit' group by s.id having units > 0")
        if not kits: return []
        rr = many("select q.sku_id, sum(q.need_lead) d, sum(case when p.on_hand + p.on_order < q.need_lead then p.on_hand + p.on_order else q.need_lead end) a "
                  "from demand_requirement q join stock_position p on p.location_id=q.location_id and p.sku_id=q.sku_id group by 1")
        avail = {r["sku_id"]: r["a"] for r in rr}; demand = {r["sku_id"]: r["d"] for r in rr}
        bom = many("select k.kit_sku_id, c.id cid, c.name cname, c.procurement_type pt, c.pack_g, c.bulk_sku_id, k.qty from kit_bom k join sku c on c.id=k.component_sku_id")
        by = {}
        for r in bom: by.setdefault(r["kit_sku_id"], []).append(r)
        out = []
        for k in kits:
            worst = None
            for c in by.get(k["id"], []):
                t = c["bulk_sku_id"] if c["pt"] == "repacked" else c["cid"]
                if t not in demand or demand[t] <= 0: continue
                ratio = min(1.0, avail.get(t, 0) / demand[t])
                if worst is None or ratio < worst[0]: worst = (ratio, c["cname"], t)
            if worst: out.append(dict(id=k["id"], code=k["code"], name=k["name"], units=int(k["units"]), coverage=float(worst[0]), limiting=worst[1], buildable=int(k["units"] * worst[0])))
        return sorted(out, key=lambda x: (x["coverage"], -x["units"]))

    @app.get("/api/demand/kits")
    def kits(u=Depends(buy_view)):
        rows = kit_rows(u); return dict(at_risk=sum(1 for r in rows if r["coverage"] < .9), total=len(rows), items=rows[:10],
                                        note="Coverage is on hand plus on order for the limiting component against all demand until a new order could land (lead time), counting every draw on it: its own sales, packs and all gift boxes. Each warehouse is counted on its own, so surplus in one place does not cover a shortage in another.")

    # ---------- store min and max ----------
    def mm_calc(rows):
        out = []
        for r in rows:
            d = r["d"]; step = 100 if r["base_uom"] == "g" else 1
            nmin = max(step, math.ceil(3 * d / step) * step); nmax = max(nmin + step, math.ceil(10 * d / step) * step)
            ch = nmax / r["max_qty"] - 1 if r["max_qty"] else 0
            out.append(dict(r, new_min=nmin, new_max=nmax, change=ch, value_change=int((nmax - r["max_qty"]) * r["price"])))
        return out

    MM = ("select a.store_id, a.sku_id, a.min_qty, a.max_qty, l.name store, l.city, s.name sku, s.base_uom, " + PRICE + " price, x.d from store_assortment a "
          "join (select store_id, sku_id, max(qty_forecast) / 7.0 d from forecast where week_start < :w4 group by 1, 2) x on x.store_id=a.store_id and x.sku_id=a.sku_id "
          "join location l on l.id=a.store_id join sku s on s.id=a.sku_id")

    @app.get("/api/demand/minmax")
    def minmax(city: str = "", size: int = 30, u=Depends(view)):
        w, p = [], dict(w4=str(an.max_date + timedelta(days=29)))
        sc = stores(u)
        if sc is not None: w.append(inlist("a.store_id", sc))
        if city: w.append("l.city=:c"); p["c"] = city
        rows = mm_calc(many(MM + ((" where " + " and ".join(w)) if w else ""), **p))
        hit = [r for r in rows if abs(r["change"]) >= .25 and abs(r["value_change"]) >= 50000]
        hit.sort(key=lambda r: -abs(r["value_change"]))
        return dict(total=len(hit), checked=len(rows), items=[{k: r[k] for k in ("store_id", "sku_id", "store", "city", "sku", "base_uom", "min_qty", "max_qty", "new_min", "new_max", "change", "value_change")} for r in hit[:max(1, min(size, 100))]],
                    note="Based on the peak forecast week of the next four weeks: minimum is 3 days, maximum is 10 days. Seasonal, so change it back after the festival. Seasonal on and off dates are not built yet.")

    @app.post("/api/demand/minmax")
    def minmax_accept(b: MinMaxIn, u=Depends(need(MM_ROLES))):
        sc = stores(u)
        if sc is not None and b.store_id not in sc: raise HTTPException(403, "That store is outside your scope")
        rows = mm_calc(many(MM + " where a.store_id=:s and a.sku_id=:k", s=b.store_id, k=b.sku_id, w4=str(an.max_date + timedelta(days=29))))
        if not rows: raise HTTPException(404, "No forecast for that store and SKU")
        r = rows[0]
        if abs(r["change"]) < .25: raise HTTPException(409, "The current limits are already close to the forecast")
        with engine.begin() as c:
            c.execute(text("update store_assortment set min_qty=:a, max_qty=:b where store_id=:s and sku_id=:k"), dict(a=r["new_min"], b=r["new_max"], s=b.store_id, k=b.sku_id))
            audit(c, u, "minmax", "store_assortment", f"{b.store_id}:{b.sku_id}", dict(min_qty=r["min_qty"], max_qty=r["max_qty"]), dict(min_qty=r["new_min"], max_qty=r["new_max"]))
        return dict(store_id=b.store_id, sku_id=b.sku_id, min_qty=r["new_min"], max_qty=r["new_max"])

    # ---------- summary and Control Tower ----------
    @app.get("/api/demand/summary")
    def summary(u=Depends(view)):
        r = run(); n = r["notes"]; sc = stores(u); f = (" where " + inlist("store_id", sc)) if sc is not None else ""
        w4 = str(an.max_date + timedelta(days=29))
        nxt = one("select coalesce(sum(value_paise), 0) v from forecast" + (f + " and" if f else " where") + " week_start < :w", w=w4)["v"]
        cur = an.q("select coalesce(sum(net), 0) from agg where date > ?" + ((" and " + inlist("store_id", sc)) if sc is not None else ""), [an.max_date - timedelta(days=28)])[0][0]
        chain = {x["week_start"]: x["v"] for x in many("select week_start, sum(value_paise) v from forecast group by 1 order by 1")}
        wk = list(chain.items()); base = n["chain_baseline_week_value"]; best = max(range(len(wk)), key=lambda i: wk[i][1])
        buy = u["role"] in BUY_VIEW
        sg = one("select count(*) n, coalesce(sum(r.value_paise), 0) v, coalesce(sum(case when r.urgency='critical' then 1 else 0 end), 0) crit from reorder_suggestion r where r.status='open'" + (f" and {inlist('r.location_id', wh_scope(u))}" if wh_scope(u) else ""))
        if not buy: sg = dict(n=0, v=0, crit=0)
        ks = kit_rows(u) if buy else []; lv = n["backtest"]["levels"]
        return dict(next4=int(nxt), last4=int(cur), delta=(nxt / cur - 1) if cur else None, peak_week=str(wk[best][0])[:10], peak_lift=wk[best][1] / base[best] - 1,
                    accuracy=1 - lv["city_category_week"]["wape"], wape_city_cat=lv["city_category_week"]["wape"], wape_flat=lv["city_category_week"]["wape_flat"], wape_store_sku=lv["store_sku_week"]["wape"], bias=lv["chain_category_week"]["bias"],
                    open=sg["n"], open_value=int(sg["v"]), critical=int(sg["crit"]), kits_at_risk=sum(1 for k in ks if k["coverage"] < .9), kits_total=len(ks), run_at=str(r["run_at"]), cutoff=n["cutoff"])

    def exceptions(f):
        out = []
        if f["store_ids"] is not None: return out
        n = one("select count(*) n, count(distinct sku_id) k from reorder_suggestion where status='open' and urgency='critical'")
        if n and n["n"]:
            top = many("select s.name, count(*) w, sum(r.value_paise) v from reorder_suggestion r join sku s on s.id=r.sku_id where r.status='open' and r.urgency='critical' group by s.name order by v desc limit 2")
            out.append(dict(type="replenish", severity="high", title=f"{n['n']} warehouse lines will run out before a new order can land", detail=f"{n['k']} SKUs across warehouses. Largest: {', '.join(f'{t['name']} ({t['w']} warehouses)' for t in top)}. Buy suggestions are open in Demand and replenishment.", ref="critical"))
        ks = kit_rows(None); bad = [k for k in ks if k["coverage"] < .9]
        if bad:
            out.append(dict(type="kits", severity="med", title=f"{len(bad)} gift boxes cannot be fully built from planned stock", detail=f"Tightest is {bad[0]['name']}, limited by {bad[0]['limiting']} at {bad[0]['coverage'] * 100:.0f}% coverage for the next eight weeks.", ref="kits"))
        return out

    return dict(exceptions=exceptions)
