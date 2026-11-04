"""Control Tower, full: headline KPIs, compare, store and SKU drill, warehouse view, exception inbox with owners, daily snapshot."""
from datetime import timedelta, date, datetime
from typing import Optional
from fastapi import Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text

COST = "case when s.base_uom='g' then {q}/1000.0*s.std_cost_paise else {q}*s.std_cost_paise end"
STATUSES = ["open", "assigned", "in_progress", "resolved"]
ASSIGNERS = {"ceo", "admin"}
DUE_DAYS = {"high": 1, "med": 3}


class InboxPatch(BaseModel):
    owner_id: Optional[int] = None
    status: Optional[str] = None
    due_date: Optional[str] = None
    unassign: bool = False


def _d(v):
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v))


def attach(app, ctx, exceptions_for, ct_guard, filters):
    me, many, one, audit, engine, an, NOW = (ctx[k] for k in ("me", "many", "one", "audit", "engine", "an", "NOW"))
    today = NOW.date()

    def scoped_ids(u):
        ids = filters(u, "", "", "", "")["store_ids"]
        return ids

    def check_store(u, sid):
        ids = scoped_ids(u)
        if ids is not None and sid not in ids: raise HTTPException(403, "This store is outside your scope")
        s = one("select id, code, name, city, region, tier, format, last_sync_at, opened_on from location where id=:i and type='store'", i=sid)
        if not s: raise HTTPException(404, "Store not found")
        return s

    def money_sums(s, e, f):
        w, p = an._where(**f)
        r = an.q("select coalesce(sum(a.net),0), coalesce(sum(a.net_ex),0), coalesce(sum(a.cogs),0) from agg a join dim_store d on d.id=a.store_id where a.date between ? and ?" + w, [s, e] + p)[0]
        return int(r[0]), float(r[1]), float(r[2])

    def id_clause(ids, col):
        return "" if ids is None else f" and {col} in ({','.join(str(int(x)) for x in ids) or '-1'})"

    # ---------- headline: stock, cover, wastage ----------
    @app.get("/api/ct/headline")
    def headline(days: int = 30, region: str = "", tier: str = "", city: str = "", fmt: str = "", u=Depends(ct_guard)):
        f = filters(u, region, tier, city, fmt)
        s, e, ps, pe, has_prev = an.period(days)
        net, ex, cogs = money_sums(s, e, f)
        ids = [r[0] for r in an.q("select d.id from dim_store d where 1=1" + an._where(**f)[0], an._where(**f)[1])]
        sv = one("select coalesce(sum(" + COST.format(q="b.qty") + "),0) v from stock_balance b join sku s on s.id=b.sku_id join location l on l.id=b.location_id "
                 "where l.type='store' and b.qty>0" + id_clause(ids, "b.location_id"))["v"]
        wv = 0
        if scoped_ids(u) is None and not (region or tier or city or fmt):
            wv = one("select coalesce(sum(" + COST.format(q="b.qty") + "),0) v from stock_balance b join sku s on s.id=b.sku_id join location l on l.id=b.location_id where l.type='warehouse' and b.qty>0")["v"]
        wst = one("select coalesce(sum(x.cost_paise),0) v from wastage_entry x where x.status!='rejected' and x.ts>=:a and x.ts<:b" + id_clause(ids, "x.location_id") if (region or tier or city or fmt or scoped_ids(u) is not None)
                  else "select coalesce(sum(x.cost_paise),0) v from wastage_entry x where x.status!='rejected' and x.ts>=:a and x.ts<:b", a=str(s), b=str(e + timedelta(days=1)))["v"]
        per_day_cogs = cogs / max(1, (e - s).days + 1)
        return dict(days=(e - s).days + 1, store_stock_cost=int(sv), warehouse_stock_cost=int(wv),
                    cover_days=(sv / per_day_cogs) if per_day_cogs else None, wastage_cost=int(wst),
                    wastage_pct_sales=(wst / ex) if ex else None, gm=(1 - cogs / ex) if ex else None,
                    gm_after_wastage=(1 - (cogs + wst) / ex) if ex else None,
                    notes=["Stock is valued at standard cost. Cover is store stock divided by daily cost of goods sold in the period.",
                           "Average bill value and units per bill need bill level history, which exists only for POS demo bills."])

    # ---------- compare ----------
    @app.get("/api/ct/compare")
    def compare(mode: str = "wow", lfl: int = 1, region: str = "", tier: str = "", city: str = "", fmt: str = "", u=Depends(ct_guard)):
        if mode not in ("wow", "mom"): raise HTTPException(422, "mode is wow or mom")
        n = 7 if mode == "wow" else 30
        e = an.max_date; s = e - timedelta(days=n - 1); pe = s - timedelta(days=1); ps = pe - timedelta(days=n - 1)
        f = filters(u, region, tier, city, fmt)
        if lfl:
            ok = [r[0] for r in an.q("select d.id from dim_store d where 1=1" + an._where(**f)[0], an._where(**f)[1])]
            have = {r[0] for r in an.q("select store_id from agg where date between ? and ? group by 1 having count(distinct date) >= ?", [ps, pe, n - 1])}
            f = dict(f, store_ids=[i for i in ok if i in have])
        w, p = an._where(**f)

        def by_cat(a, b):
            return {r[0]: (r[1], r[2], r[3]) for r in an.q("select a.cat, sum(a.net), sum(a.net_ex), sum(a.cogs) from agg a join dim_store d on d.id=a.store_id where a.date between ? and ?" + w + " group by 1", [a, b] + p)}
        cur, prev = by_cat(s, e), by_cat(ps, pe)
        rows = []
        for c in sorted(cur, key=lambda c: -cur[c][0]):
            pv = prev.get(c)
            rows.append(dict(code=c, name=an.cats[c], net=int(cur[c][0]), prev=int(pv[0]) if pv else None, change=(cur[c][0] / pv[0] - 1) if pv and pv[0] else None,
                             gm=(1 - cur[c][2] / cur[c][1]) if cur[c][1] else None, gm_prev=(1 - pv[2] / pv[1]) if pv and pv[1] else None))
        tc, tp = sum(v[0] for v in cur.values()), sum(v[0] for v in prev.values())
        gx, gc = sum(v[1] for v in cur.values()), sum(v[2] for v in cur.values())
        px, pc = sum(v[1] for v in prev.values()), sum(v[2] for v in prev.values())
        return dict(mode=mode, like_for_like=bool(lfl), stores=len(f["store_ids"]) if f["store_ids"] is not None else None, start=str(s), end=str(e), prev_start=str(ps), prev_end=str(pe),
                    net=int(tc), prev=int(tp), change=(tc / tp - 1) if tp else None, gm=(1 - gc / gx) if gx else None, gm_prev=(1 - pc / px) if px else None, categories=rows,
                    note="Same festival last year is not available, the history starts in May 2026. Like for like keeps stores that traded on nearly every day of both periods.")

    # ---------- drill: store, SKU ----------
    @app.get("/api/ct/store/{sid}")
    def store_drill(sid: int, days: int = 30, u=Depends(ct_guard)):
        st = check_store(u, sid); f = dict(region=None, tier=None, city=None, store_ids=[sid])
        s, e, ps, pe, has_prev = an.period(days)
        cur = an._sum(s, e, f); prev = an._sum(ps, pe, f) if has_prev else None
        top = an.q("select k.id, k.name, k.cat_name, sum(x.qty), sum(x.net_paise), sum(x.cogs_paise) from sales x join dim_sku k on k.id=x.sku_id where x.store_id=? and x.date between ? and ? group by all order by 5 desc limit 12", [sid, s, e])
        so = an.q("select k.id, k.name, o.start_date, o.end_date from stockouts o join dim_sku k on k.id=o.sku_id where o.store_id=? and o.end_date >= ? order by o.start_date desc limit 10", [sid, s])
        mix = an.q("select a.cat, sum(a.net) from agg a where a.store_id=? and a.date between ? and ? group by 1 order by 2 desc", [sid, s, e]); tot = sum(r[1] for r in mix) or 1
        trend = an.q("select a.date, sum(a.net) from agg a where a.store_id=? and a.date between ? and ? group by 1 order by 1", [sid, s, e])
        waste = one("select coalesce(sum(cost_paise),0) v from wastage_entry where location_id=:l and status!='rejected' and ts>=:a", l=sid, a=str(s))["v"]
        pos = one("select count(*) bills, coalesce(sum(net_paise),0) sales_paise, coalesce(avg(net_paise),0) avg_bill_paise from sale where store_id=:i and business_date between :a and :b and status!='void'", i=sid, a=str(s), b=str(e))
        pos_lines = one("select coalesce(sum(x.lines),0) line_count, coalesce(avg(x.lines),0) lines_per_bill from (select s.id, count(sl.id) lines from sale s left join sale_line sl on sl.sale_id=s.id where s.store_id=:i and s.business_date between :a and :b and s.status!='void' group by s.id) x", i=sid, a=str(s), b=str(e))
        pos_returns = one("select count(*) n, coalesce(sum(refund_paise),0) v from sale_return where store_id=:i and status='approved' and created_at>=:a and created_at<:b", i=sid, a=str(s), b=str(e + timedelta(days=1)))
        bills = many("select id, invoice_no, business_date, net_paise, status from sale where store_id=:i and status!='void' order by created_at_device desc limit 8", i=sid)
        mine = [x for x in exceptions_for(filters(u, "", "", "", "")) if x.get("type") in ("sync", "drop") and x.get("ref") == sid]
        return dict(store=dict(st, last_sync_at=str(st["last_sync_at"]) if st["last_sync_at"] else None, opened_on=str(st["opened_on"]) if st["opened_on"] else None),
                    days=(e - s).days + 1, current=cur, previous=prev, wastage_cost=int(waste),
                    pos_summary=dict(source="POS transactions only", start=str(s), end=str(e), bills=int(pos["bills"]), sales_paise=int(pos["sales_paise"]),
                                     avg_bill_paise=int(pos["avg_bill_paise"]), lines=int(pos_lines["line_count"]), lines_per_bill=float(pos_lines["lines_per_bill"]),
                                     approved_returns=int(pos_returns["n"]), refunds_paise=int(pos_returns["v"]),
                                     separate_from_parquet=True),
                    trend=[dict(date=str(r[0]), net=int(r[1])) for r in trend],
                    mix=[dict(code=r[0], name=an.cats[r[0]], net=int(r[1]), share=r[1] / tot) for r in mix],
                    top_skus=[dict(id=r[0], name=r[1], cat=r[2], qty=int(r[3]), net=int(r[4]), gm=(1 - r[5] / (r[4] / 1.05)) if r[4] else None) for r in top],
                    stockouts=[dict(sku_id=r[0], name=r[1], start=str(r[2]), end=str(r[3])) for r in so],
                    exceptions=mine, bills=[dict(b, business_date=str(b["business_date"])) for b in bills],
                    bills_note="Bill level data exists only for POS demo bills, so most stores show none. History is store by SKU by day.")

    @app.get("/api/ct/bills")
    def pos_bills(store_id: Optional[int] = None, start: str = "", end: str = "", q: str = "", page: int = 1, size: int = 25, u=Depends(ct_guard)):
        ids = scoped_ids(u)
        if store_id is not None:
            check_store(u, store_id)
            ids = [store_id]
        page, size = max(1, page), max(5, min(size, 100))
        where, params = ["s.status!='void'"], dict(lim=size, off=(page - 1) * size)
        if ids is not None:
            where.append("s.store_id in (%s)" % (",".join(str(int(x)) for x in ids) or "-1"))
        if start:
            try: date.fromisoformat(start)
            except ValueError: raise HTTPException(422, "start must be YYYY-MM-DD")
            where.append("s.business_date>=:start"); params["start"] = start
        if end:
            try: date.fromisoformat(end)
            except ValueError: raise HTTPException(422, "end must be YYYY-MM-DD")
            where.append("s.business_date<=:end"); params["end"] = end
        if start and end and start > end: raise HTTPException(422, "start must not be after end")
        if q.strip():
            where.append("(lower(s.invoice_no) like :q or lower(l.name) like :q)")
            params["q"] = "%" + q.strip().lower() + "%"
        clause = " where " + " and ".join(where)
        total = one("select count(*) n from sale s join location l on l.id=s.store_id" + clause, **params)["n"]
        rows = many("select s.id,s.invoice_no,s.business_date,s.net_paise,s.status,s.store_id,l.name store_name from sale s join location l on l.id=s.store_id" + clause +
                    " order by s.business_date desc,s.created_at_device desc limit :lim offset :off", **params)
        return dict(total=total, page=page, size=size, rows=[dict(x, business_date=str(x["business_date"])) for x in rows],
                    source="POS transactions only; excluded from aggregate Parquet KPIs")

    @app.get("/api/ct/bills/{bill_id}")
    def pos_bill_detail(bill_id: str, u=Depends(ct_guard)):
        b = one("select s.id,s.invoice_no,s.store_id,s.business_date,s.created_at_device,s.received_at,s.status,s.gross_paise,s.discount_paise,s.tax_paise,s.net_paise,s.round_off_paise,s.void_reason,s.customer_id,l.code store_code,l.name store_name,au.name cashier "
                "from sale s join location l on l.id=s.store_id left join app_user au on au.id=s.cashier_id where s.id=:i", i=bill_id)
        if not b: raise HTTPException(404, "Bill not found")
        ids = scoped_ids(u)
        if ids is not None and b["store_id"] not in ids: raise HTTPException(403, "This bill is outside your scope")
        lines = many("select sl.id,sl.sku_id,sk.code,sk.name,sk.base_uom,sl.qty,sl.unit_price_paise,sl.discount_paise,sl.tax_rate,sl.tax_paise,sl.net_paise "
                     "from sale_line sl join sku sk on sk.id=sl.sku_id where sl.sale_id=:i order by sl.id", i=bill_id)
        payments = many("select mode,amount_paise,status,ref from payment where sale_id=:i order by id", i=bill_id)
        returns = many("select r.id,r.return_no,r.created_at,r.reason,r.status,r.refund_paise,coalesce(sum(rl.qty),0) returned_qty "
                       "from sale_return r left join sale_return_line rl on rl.return_id=r.id where r.sale_id=:i group by r.id order by r.created_at desc", i=bill_id)
        for r in returns: r["created_at"] = str(r["created_at"])
        for x in lines:
            x["qty_label"] = (f'{x["qty"] / 1000:g} kg' if x["base_uom"] == "g" else f'{x["qty"]} units')
        approved_refunds = sum(int(r["refund_paise"] or 0) for r in returns if r["status"] == "approved")
        b["business_date"] = str(b["business_date"]); b["created_at_device"] = str(b["created_at_device"])
        b["received_at"] = str(b["received_at"]) if b["received_at"] else None
        return dict(bill=b, lines=lines, payments=payments, returns=returns, approved_refunds_paise=approved_refunds,
                    retained_value_paise=int(b["net_paise"] or 0) - approved_refunds,
                    source="POS transaction detail; not added to aggregate Parquet history")

    @app.get("/api/ct/store/{sid}/sku/{kid}")
    def sku_drill(sid: int, kid: int, days: int = 60, u=Depends(ct_guard)):
        st = check_store(u, sid)
        sk = one("select s.id, s.code, s.name, s.base_uom, s.pack_g, s.std_cost_paise, coalesce(s.mrp_paise, s.price_per_kg_paise) price from sku s where s.id=:i", i=kid)
        if not sk: raise HTTPException(404, "SKU not found")
        s = an.max_date - timedelta(days=days - 1)
        rows = an.q("select date, qty, net_paise from sales where store_id=? and sku_id=? and date >= ? order by 1", [sid, kid, s])
        so = an.q("select start_date, end_date from stockouts where store_id=? and sku_id=? and end_date >= ? order by 1", [sid, kid, s])
        bal = one("select coalesce(sum(qty),0) q from stock_balance where location_id=:l and sku_id=:k", l=sid, k=kid)["q"]
        wst = many("select ts, qty, reason, cost_paise, status from wastage_entry where location_id=:l and sku_id=:k order by ts desc limit 8", l=sid, k=kid)
        return dict(store=dict(id=st["id"], name=st["name"], city=st["city"]), sku=sk, days=days, on_hand=int(bal),
                    daily=[dict(date=str(r[0]), qty=int(r[1]), net=int(r[2])) for r in rows], stockouts=[dict(start=str(r[0]), end=str(r[1])) for r in so],
                    wastage=[dict(w, ts=str(w["ts"])) for w in wst], qty_unit="g" if sk["base_uom"] == "g" else "units")

    # ---------- warehouse view ----------
    @app.get("/api/ct/warehouses")
    def warehouses(u=Depends(ct_guard)):
        if u["scope_type"] == "store": raise HTTPException(403, "Warehouse view is for head office and warehouse teams")
        only = u["scope_id"] if u["scope_type"] == "warehouse" else None
        wh = many("select l.id, l.code, l.name, l.city, l.region, (select count(*) from location x where x.serving_wh_id=l.id and x.type='store') stores from location l where l.type='warehouse'" + (f" and l.id={int(only)}" if only else "") + " order by l.id")
        d30 = str(today + timedelta(days=30))
        val = {r["location_id"]: r for r in many("select b.location_id, sum(" + COST.format(q="b.qty") + ") v, count(distinct b.sku_id) skus, sum(case when t.expiry_date <= :d then " + COST.format(q="b.qty") + " else 0 end) exp30 "
                                                 "from stock_balance b join sku s on s.id=b.sku_id left join batch t on t.id=b.batch_id join location l on l.id=b.location_id where l.type='warehouse' and b.qty>0 group by b.location_id", d=d30)}
        age = {}
        for r in many("select b.location_id, t.received_on, sum(" + COST.format(q="b.qty") + ") v from stock_balance b join sku s on s.id=b.sku_id left join batch t on t.id=b.batch_id join location l on l.id=b.location_id where l.type='warehouse' and b.qty>0 group by b.location_id, t.received_on"):
            if r["received_on"] is None: n = 0
            else:
                ro = r["received_on"] if isinstance(r["received_on"], date) else date.fromisoformat(str(r["received_on"])[:10]); n = (today - ro).days
            k = "d0_30" if n <= 30 else "d31_60" if n <= 60 else "d61_90" if n <= 90 else "d90p"
            a = age.setdefault(r["location_id"], dict(d0_30=0, d31_60=0, d61_90=0, d90p=0)); a[k] += float(r["v"] or 0)
        tr = {}
        for r in many("select from_location_id w, status, count(*) n, min(coalesce(dispatched_at, created_at)) oldest from transfer group by from_location_id, status"):
            tr.setdefault(r["w"], {})[r["status"]] = dict(n=r["n"], oldest=str(r["oldest"])[:10] if r["oldest"] else None)
        shr = {r["location_id"]: int(r["c"]) for r in many("select location_id, sum(cost_paise) c from wastage_entry where status!='rejected' and ts>=:a group by location_id", a=str(today - timedelta(days=29)))}
        out = []
        for w in wh:
            v = val.get(w["id"], {}); t = tr.get(w["id"], {}); it = t.get("in_transit")
            late = None
            if it and it["oldest"]: late = (today - date.fromisoformat(it["oldest"])).days
            out.append(dict(w, stock_cost=int(v.get("v") or 0), skus=v.get("skus") or 0, expiring_30d=int(v.get("exp30") or 0), ageing={k: int(round(v)) for k, v in age.get(w["id"], dict(d0_30=0, d31_60=0, d61_90=0, d90p=0)).items()},
                            in_transit=it["n"] if it else 0, oldest_in_transit_days=late, draft=t.get("draft", {}).get("n", 0), received=t.get("received", {}).get("n", 0), loss_30d=shr.get(w["id"], 0)))
        med = sorted(x["loss_30d"] for x in out)[len(out) // 2] if out else 0
        for x in out: x["loss_flag"] = bool(med and x["loss_30d"] >= 2 * med)
        return dict(rows=out, as_of=str(today), note="Ageing is by receipt date of the batch. Loss is approved or pending wastage entries at cost over 30 days, flagged at twice the median warehouse.")

    # ---------- exception inbox with owners ----------
    def sync_inbox():
        cur = exceptions_for(filters(dict(scope_type="all", scope_id=None, role="admin"), "", "", "", ""))
        seen = set()
        with engine.begin() as c:
            have = {(r["type"], r["ref_id"]): r for r in [dict(x._mapping) for x in c.execute(text("select id, type, ref_id, status from exception_item")).fetchall()]}
            for x in cur:
                rid = str(x.get("ref")) if x.get("ref") is not None else x["title"][:40]; key = (x["type"], rid); seen.add(key)
                loc = x["ref"] if x["type"] in ("sync", "drop") and isinstance(x.get("ref"), int) else None
                if key in have:
                    c.execute(text("update exception_item set title=:t, severity=:s, location_id=:l, status=case when status='cleared' then 'open' else status end where id=:i"), dict(t=x["title"][:160], s=x["severity"], l=loc, i=have[key]["id"]))
                else:
                    c.execute(text("insert into exception_item(ts, type, severity, location_id, ref_type, ref_id, title, status, owner_id, due_date) values (:ts,:ty,:sv,:l,:rt,:ri,:t,'open',null,:d)"),
                              dict(ts=NOW, ty=x["type"], sv=x["severity"], l=loc, rt=x["type"], ri=rid, t=x["title"][:160], d=str(today + timedelta(days=DUE_DAYS.get(x["severity"], 3)))))
            for key, r in have.items():
                if key not in seen and r["status"] not in ("resolved", "cleared"):
                    c.execute(text("update exception_item set status='cleared' where id=:i"), dict(i=r["id"]))

    def inbox_rows(u, status):
        ids = scoped_ids(u)
        q = ("select e.id, e.ts, e.type, e.severity, e.location_id, e.title, e.status, e.owner_id, e.due_date, o.name owner_name, l.name store_name from exception_item e "
             "left join app_user o on o.id=e.owner_id left join location l on l.id=e.location_id where e.status!='cleared'")
        if status == "active": q += " and e.status!='resolved'"
        elif status in STATUSES: q += f" and e.status='{status}'"
        if ids is not None: q += " and e.location_id in (%s)" % (",".join(str(int(i)) for i in ids) or "-1")
        rows = many(q + " order by case e.severity when 'high' then 0 else 1 end, e.due_date, e.id")
        for r in rows:
            r["due_date"] = str(r["due_date"])[:10] if r["due_date"] else None; r["ts"] = str(r["ts"])
            r["overdue"] = bool(r["due_date"] and r["due_date"] < str(today) and r["status"] != "resolved")
        return rows

    @app.get("/api/ct/inbox")
    def inbox(status: str = "active", u=Depends(ct_guard)):
        sync_inbox()
        rows = inbox_rows(u, status)
        return dict(rows=rows, open=sum(1 for r in rows if r["status"] != "resolved"), overdue=sum(1 for r in rows if r["overdue"]), unassigned=sum(1 for r in rows if r["status"] == "open" and not r["owner_id"]),
                    can_assign=u["role"] in ASSIGNERS)

    @app.get("/api/ct/owners")
    def owners(u=Depends(ct_guard)):
        return many("select u.id, u.name, r.code role from app_user u join role r on r.id=u.role_id where u.active=1 and r.code not in ('cashier') order by u.id")

    @app.patch("/api/ct/inbox/{iid}")
    def inbox_patch(iid: int, b: InboxPatch, u=Depends(ct_guard)):
        it = one("select id, status, owner_id, due_date, location_id from exception_item where id=:i", i=iid)
        if not it: raise HTTPException(404, "Item not found")
        ids = scoped_ids(u)
        if ids is not None and it["location_id"] not in ids: raise HTTPException(403, "This item is outside your scope")
        boss = u["role"] in ASSIGNERS; mine = it["owner_id"] == u["id"]
        ch = {}
        if b.owner_id is not None or b.unassign or b.due_date is not None:
            if not boss: raise HTTPException(403, "Only the CEO or admin can assign owners and due dates")
        if b.unassign: ch["owner_id"] = None
        if b.owner_id is not None:
            if not one("select id from app_user where id=:i and active=1", i=b.owner_id): raise HTTPException(422, "Unknown owner")
            ch["owner_id"] = b.owner_id
            if b.status is None and it["status"] == "open": ch["status"] = "assigned"
        if b.due_date is not None:
            try: d = date.fromisoformat(b.due_date)
            except ValueError: raise HTTPException(422, "Due date must be YYYY-MM-DD")
            if d < today: raise HTTPException(422, "Due date is in the past")
            ch["due_date"] = str(d)
        if b.status is not None:
            if b.status not in STATUSES: raise HTTPException(422, "Unknown status")
            if not (boss or mine): raise HTTPException(403, "Only the owner, the CEO or admin can change the status")
            if b.status in ("assigned", "in_progress") and not (ch.get("owner_id") or it["owner_id"]): raise HTTPException(422, "Assign an owner first")
            ch["status"] = b.status
        if not ch: raise HTTPException(422, "Nothing to change")
        with engine.begin() as c:
            c.execute(text("update exception_item set " + ", ".join(f"{k}=:{k}" for k in ch) + " where id=:i"), dict(ch, i=iid))
            audit(c, u, "exception_update", "exception_item", iid, {k: str(it.get(k)) for k in ch if k in it}, {k: str(v) for k, v in ch.items()})
        return dict(id=iid, **ch)

    # ---------- daily snapshot ----------
    @app.get("/api/ct/snapshot")
    def snapshot(save: int = 0, u=Depends(ct_guard)):
        f = filters(u, "", "", "", "")
        s = an.summary(1, **f); w = an.summary(7, **f); c = s["current"]
        ex = exceptions_for(f); hi = [x for x in ex if x["severity"] == "high"]
        lines = [f"DFH morning summary for {s['end']}", f"Net sales Rs {c['net'] / 100:,.0f} ({'n/a' if s['delta']['net'] is None else f'{s['delta']['net'] * 100:+.1f}%'} vs the day before)",
                 f"Last 7 days Rs {w['current']['net'] / 100:,.0f} ({'n/a' if w['delta']['net'] is None else f'{w['delta']['net'] * 100:+.1f}%'} vs the 7 before), gross margin {w['current']['gm'] * 100:.1f}%",
                 f"Stockout events in the last 7 days: {w['stockouts']['events']}", f"{len(ex)} exceptions open, {len(hi)} high"] + [f"- {x['title']}" for x in hi[:5]]
        if save and u["role"] in ASSIGNERS:
            with engine.begin() as cn:
                cn.execute(text("delete from kpi_snapshot where date=:d and dim_type='chain'"), dict(d=str(an.max_date)))
                for k, v in (("net_paise", c["net"]), ("gm", c["gm"]), ("exceptions_open", len(ex)), ("exceptions_high", len(hi))):
                    cn.execute(text("insert into kpi_snapshot(date, kpi, dim_type, dim_id, value) values (:d,:k,'chain',null,:v)"), dict(d=str(an.max_date), k=k, v=float(v)))
        return dict(date=str(an.max_date), text="\n".join(lines), saved=bool(save and u["role"] in ASSIGNERS), delivery="Copy into WhatsApp or email. Scheduled sending is not built.")
