"""Module 7 API: cash and settlement reconciliation, GST and input credit against GSTR-2B, month close and lock, payables, journal and export.
Money is in paise. GST rates are assumed (5 and 18 percent). Filing is tracked here, not done through a GSP."""
import csv, io
from datetime import date, timedelta
from typing import Optional
from fastapi import Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

FIN = {"ceo", "finance", "admin"}; CASH_VIEW = FIN | {"store_manager"}; PAY_VIEW = FIN | {"procurement"}; ANY = CASH_VIEW | PAY_VIEW
PERIODS = [f"2026-{m:02d}" for m in range(5, 11)]
THRESH = 100_000            # a day close short by Rs 1,000 or more counts as a real shortage
WO_LIMIT = 2_500_000        # write-offs above Rs 25,000 need the CEO or admin
BIG = {"ceo", "admin"}
money = lambda p: f"Rs {abs(p) / 100:,.0f}"


class Resolve(BaseModel):
    action: str
    note: str


class Lock(BaseModel):
    entity_id: int
    period: str


def attach(app, ctx):
    me, need, many, one, audit, engine, NOW = (ctx[k] for k in ("me", "need", "many", "one", "audit", "engine", "NOW"))
    from sqlalchemy import text
    END = NOW.date()

    def stores(u):
        return [u["scope_id"]] if u["scope_type"] == "store" else None

    def inl(col, ids): return f" and {col} in ({','.join(str(int(x)) for x in ids) or '-1'})"

    def span(days): return str(END - timedelta(days=max(1, min(days, 184)) - 1))

    def period_bounds(p):
        try: y, m = int(p[:4]), int(p[5:7]); first = date(y, m, 1)
        except Exception: raise HTTPException(422, "Period must look like 2026-09")
        return first, date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)

    def entity(e):
        r = one("select id, code, name, country, state, currency from legal_entity where id=:i", i=e)
        if not r: raise HTTPException(404, "Unknown entity")
        return r

    # ---------- cash ----------
    def cash_rows(days, sc=None):
        s = span(days); w = inl("d.store_id", sc) if sc is not None else ""
        base = many("select d.store_id id, l.name, l.city, count(*) closes, coalesce(sum(case when d.variance_paise<0 then d.variance_paise end),0) short_total, coalesce(sum(d.variance_paise),0) net, "
                    "sum(case when d.variance_paise<=-:t then 1 else 0 end) short_days from day_close d join location l on l.id=d.store_id where d.business_date>=:s and d.status!='open'" + w + " group by d.store_id, l.name, l.city", s=s, t=THRESH)
        wk = {}
        for r in many("select d.store_id, d.business_date from day_close d where d.variance_paise<=-:t and d.business_date>=:s" + w, s=s, t=THRESH):
            if date.fromisoformat(str(r["business_date"])[:10]).weekday() >= 5: wk[r["store_id"]] = wk.get(r["store_id"], 0) + 1
        for r in base:
            r["weekend_short_days"] = wk.get(r["id"], 0)
            r["flag"] = r["weekend_short_days"] >= 4 and r["weekend_short_days"] >= 0.8 * r["short_days"]
        return sorted(base, key=lambda r: r["short_total"])

    @app.get("/api/finance/options")
    def options(u=Depends(need(ANY))):
        r = u["role"]
        return dict(entities=many("select id, code, name, country, state, currency from legal_entity order by id"), periods=PERIODS, end=str(END), wo_limit=WO_LIMIT,
                    can_cash=r in CASH_VIEW, can_gst=r in FIN, can_pay=r in PAY_VIEW, can_resolve=r in FIN, can_lock=r in FIN, scoped=u["scope_type"] == "store")

    @app.get("/api/finance/summary")
    def summary(days: int = 30, u=Depends(need(CASH_VIEW))):
        sc = stores(u); s = span(days); w = inl("store_id", sc) if sc is not None else ""
        dc = one("select count(*) n, sum(case when status='open' then 1 else 0 end) opn, coalesce(sum(variance_paise),0) net, coalesce(sum(case when variance_paise<0 then variance_paise end),0) short, "
                 "sum(case when variance_paise<=-:t then 1 else 0 end) big from day_close where business_date>=:s" + w, s=s, t=THRESH)
        se = one("select coalesce(sum(case when status!='in_transit' then expected_paise end),0) expected, coalesce(sum(received_paise),0) received, coalesce(sum(fee_paise),0) fee, "
                 "coalesce(sum(case when status='in_transit' then expected_paise end),0) transit, coalesce(sum(case when status in ('short','held') then expected_paise-fee_paise-received_paise end),0) gap "
                 "from settlement where business_date>=:s" + w, s=s)
        rc = one("select count(*) n, coalesce(sum(-amount_paise),0) v from recon_item where status='open'" + w)
        out = dict(days=days, cash=dict(closes=dc["n"], not_closed=dc["opn"] or 0, net=dc["net"], short=dc["short"], big=dc["big"] or 0), settlements=se, recon=rc)
        if u["role"] in FIN:
            pay = payables(); out["payables"] = dict(total=pay["total"], overdue=pay["overdue"])
            out["itc_at_risk"] = sum(r["at_risk"] for e in many("select id from legal_entity where state is not null") for r in gst_rows(e["id"]))
        return out

    @app.get("/api/finance/cash/stores")
    def cash_stores(days: int = 60, flagged: int = 0, u=Depends(need(CASH_VIEW))):
        rows = cash_rows(days, stores(u)); fl = [r for r in rows if r["flag"]]
        return dict(days=days, rows=(fl if flagged else [r for r in rows if r["short_total"] < 0][:15]), flagged=len(fl), stores_short=sum(1 for r in rows if r["short_days"]), short_total=sum(r["short_total"] for r in rows))

    @app.get("/api/finance/cash/store/{sid}")
    def cash_store(sid: int, days: int = 60, u=Depends(need(CASH_VIEW))):
        sc = stores(u)
        if sc is not None and sid not in sc: raise HTTPException(403, "That store is outside your scope")
        loc = one("select id, name, city from location where id=:i and type='store'", i=sid)
        if not loc: raise HTTPException(404, "Unknown store")
        rows = many("select business_date, expected_cash_paise, counted_cash_paise, variance_paise, status from day_close where store_id=:i and business_date>=:s order by business_date desc", i=sid, s=span(days))
        for r in rows: r["weekday"] = date.fromisoformat(str(r["business_date"])[:10]).strftime("%a")
        return dict(store=loc, rows=rows)

    @app.get("/api/finance/settlements")
    def settlements(days: int = 30, u=Depends(need(CASH_VIEW))):
        sc = stores(u); s = span(days); w = inl("t.store_id", sc) if sc is not None else ""
        modes = many("select t.mode, count(*) n, coalesce(sum(case when t.status!='in_transit' then t.expected_paise end),0) expected, coalesce(sum(t.received_paise),0) received, coalesce(sum(t.fee_paise),0) fee, "
                     "coalesce(sum(case when t.status='in_transit' then t.expected_paise end),0) transit, coalesce(sum(case when t.status in ('short','held') then t.expected_paise-t.fee_paise-t.received_paise end),0) gap, "
                     "sum(case when t.status in ('short','held') then 1 else 0 end) exceptions from settlement t where t.business_date>=:s" + w + " group by t.mode order by t.mode", s=s)
        items = many("select t.id, t.ref, t.mode, t.business_date, t.settle_date, t.expected_paise, t.received_paise, t.fee_paise, t.status, l.name, l.city from settlement t join location l on l.id=t.store_id "
                     "where t.status in ('short','held') and t.business_date>=:s" + w + " order by t.business_date desc, t.id desc limit 20", s=s)
        return dict(modes=modes, items=items)

    @app.get("/api/finance/recon")
    def recon(status: str = "open", source: str = "", page: int = 1, size: int = 12, u=Depends(need(CASH_VIEW))):
        sc = stores(u); w = (inl("r.store_id", sc) if sc is not None else "") + (" and r.status=:st" if status else "") + (" and r.source=:so" if source else "")
        p = dict(st=status, so=source)
        total = one("select count(*) n from recon_item r where 1=1" + w, **p)["n"]
        items = many("select r.id, r.source, r.source_ref, r.store_id, r.business_date, r.amount_paise, r.status, r.note, r.resolved_at, l.name, l.city from recon_item r join location l on l.id=r.store_id where 1=1" + w +
                     " order by r.business_date desc, r.id desc limit :n offset :o", n=size, o=(max(page, 1) - 1) * size, **p)
        for r in items: r["age"] = (END - date.fromisoformat(str(r["business_date"])[:10])).days
        facets = many("select source, status, count(*) n, coalesce(sum(-amount_paise),0) v from recon_item r where 1=1" + (inl("r.store_id", sc) if sc is not None else "") + " group by source, status")
        return dict(total=total, items=items, facets=facets, page=page, size=size)

    def post_jv(c, ent, kind, ref, narr, ls):
        e = c.execute(text("select code from legal_entity where id=:i"), dict(i=ent)).scalar()
        jid = c.execute(text("insert into journal_entry(jv_no, date, entity_id, source_doc_type, source_doc_id, narration) values (:n,:d,:e,:k,:r,:m) returning id"),
                        dict(n=f"{e}/{kind}/{ref}", d=str(END), e=ent, k=kind.lower(), r=str(ref), m=narr)).scalar_one()
        for a, dr, cr in ls: c.execute(text("insert into journal_line(jv_id, account_code, debit_paise, credit_paise, location_id) values (:j,:a,:d,:c,null)"), dict(j=jid, a=a, d=dr, c=cr))

    @app.post("/api/finance/recon/{iid}/resolve")
    def resolve(iid: int, b: Resolve, u=Depends(need(FIN))):
        it = one("select r.id, r.source, r.source_ref, r.store_id, r.amount_paise, r.status, l.entity_id from recon_item r join location l on l.id=r.store_id where r.id=:i", i=iid)
        if not it: raise HTTPException(404, "Item not found")
        if it["status"] != "open": raise HTTPException(409, "This item is already resolved")
        if b.action not in ("match", "write_off"): raise HTTPException(422, "Action must be match or write_off")
        if len((b.note or "").strip()) < 8: raise HTTPException(422, "Say why in a few words")
        amt = -it["amount_paise"]
        if b.action == "write_off" and amt > WO_LIMIT and u["role"] not in BIG: raise HTTPException(403, f"Write-offs above {money(WO_LIMIT)} need the CEO or admin")
        if one("select 1 x from period_lock where entity_id=:e and period=:p", e=it["entity_id"], p=str(END)[:7]): raise HTTPException(409, f"The {str(END)[:7]} books are locked for this entity")
        new = "matched" if b.action == "match" else "written_off"
        with engine.begin() as c:
            c.execute(text("update recon_item set status=:s, note=:n, resolved_by=:u, resolved_at=:t, matched_to=:m where id=:i"), dict(s=new, n=b.note.strip(), u=u["id"], t=NOW, m=f"MANUAL-{iid}" if new == "matched" else None, i=iid))
            if it["source"] == "settlement":
                post_jv(c, it["entity_id"], "ADJ", f"R{iid}", "Settlement shortfall cleared", [("1030" if new == "matched" else "5400", amt, 0), ("1050", 0, amt)])
            audit(c, u, f"recon.{b.action}", "recon_item", iid, dict(status="open"), dict(status=new, note=b.note.strip(), amount=amt))
        return dict(id=iid, status=new)

    # ---------- GST and input credit ----------
    def gst_rows(e):
        out = {p: dict(period=p, entity_id=e, taxable=0, tax5=0, tax18=0, output_tax=0, books_itc=0, books_n=0, portal_itc=0, unbooked_n=0, unbooked_tax=0, missing_n=0, missing_tax=0, mismatch_n=0, mismatch_tax=0) for p in PERIODS}
        for r in many("select substr(cast(j.date as text),1,7) p, l.account_code a, sum(l.credit_paise - l.debit_paise) v from journal_line l join journal_entry j on j.id=l.jv_id where j.entity_id=:e and l.account_code in ('4000','4001','2100','2101') group by 1,2", e=e):
            o = out.get(r["p"])
            if not o: continue
            if r["a"] in ("4000", "4001"): o["taxable"] += r["v"]
            else: o["output_tax"] += r["v"]; o["tax5" if r["a"] == "2100" else "tax18"] = r["v"]
        for r in many("select substr(cast(inv_date as text),1,7) p, count(*) n, sum(tax_minor) t from vendor_invoice where entity_id=:e and tax_minor>0 group by 1", e=e):
            if r["p"] in out: out[r["p"]].update(books_n=r["n"], books_itc=r["t"])
        avail = set()
        for r in many("select period p, status, count(*) n, sum(tax_paise) t from gstr2b_line where entity_id=:e group by 1,2", e=e):
            avail.add(r["p"]); o = out[r["p"]]
            if r["status"] == "unbooked": o["unbooked_n"], o["unbooked_tax"] = r["n"], r["t"]
            else: o["portal_itc"] += r["t"]
        for r in many("select substr(cast(i.inv_date as text),1,7) p, count(*) n, sum(i.tax_minor) t from vendor_invoice i where i.entity_id=:e and i.tax_minor>0 and not exists (select 1 from gstr2b_line g where g.matched_invoice_id=i.id) group by 1", e=e):
            if r["p"] in avail: out[r["p"]].update(missing_n=r["n"], missing_tax=r["t"])
        for r in many("select g.period p, count(*) n, sum(i.tax_minor - g.tax_paise) t from gstr2b_line g join vendor_invoice i on i.id=g.matched_invoice_id where g.entity_id=:e and g.status='mismatch' group by 1", e=e):
            out[r["p"]].update(mismatch_n=r["n"], mismatch_tax=r["t"])
        st = {r["period"]: r for r in many("select period, status, locked_at from gst_return_period where entity_id=:e", e=e)}
        lk = {r["period"] for r in many("select period from period_lock where entity_id=:e", e=e)}
        for p, o in out.items():
            y, m = int(p[:4]), int(p[5:]); o["available"] = p in avail; o["at_risk"] = o["missing_tax"] + o["mismatch_tax"]
            o["claimable"] = o["portal_itc"] if o["available"] else o["books_itc"]; o["net_payable"] = o["output_tax"] - o["claimable"]
            o["status"] = st.get(p, {}).get("status", "open"); o["due"] = str(date(y + (m == 12), m % 12 + 1, 20)); o["locked"] = p in lk
        return list(out.values())

    @app.get("/api/finance/gst")
    def gst(entity_id: int, u=Depends(need(FIN))):
        e = entity(entity_id)
        if not e["state"]: raise HTTPException(404, "GST applies to Indian entities. VAT for Gulf entities is not modelled.")
        return dict(entity=e, rows=gst_rows(entity_id))

    @app.get("/api/finance/gst/detail")
    def gst_detail(entity_id: int, period: str, u=Depends(need(FIN))):
        entity(entity_id); period_bounds(period)
        base = "i.entity_id=:e and i.tax_minor>0 and substr(cast(i.inv_date as text),1,7)=:p"
        avail = one("select count(*) n from gstr2b_line where entity_id=:e and period=:p", e=entity_id, p=period)["n"] > 0
        missing = many("select i.id, i.inv_no, i.inv_date, v.name vendor, i.taxable_minor, i.tax_minor from vendor_invoice i join vendor v on v.id=i.vendor_id where " + base + " and not exists (select 1 from gstr2b_line g where g.matched_invoice_id=i.id) order by i.tax_minor desc", e=entity_id, p=period) if avail else []
        mism = many("select i.inv_no, v.name vendor, i.tax_minor books, g.tax_paise portal, i.tax_minor - g.tax_paise diff from gstr2b_line g join vendor_invoice i on i.id=g.matched_invoice_id join vendor v on v.id=i.vendor_id where g.entity_id=:e and g.period=:p and g.status='mismatch'", e=entity_id, p=period)
        unb = many("select g.inv_no, g.supplier_gstin, g.taxable_paise, g.tax_paise from gstr2b_line g where g.entity_id=:e and g.period=:p and g.status='unbooked'", e=entity_id, p=period)
        return dict(available=avail, missing=missing, mismatch=mism, unbooked=unb)

    # ---------- month close ----------
    def checklist(e, p):
        first, last = period_bounds(p); f, l = str(first), str(last); sub = " and store_id in (select id from location where entity_id=:e)"
        q = dict(e=e, f=f, l=l)
        opn = one("select count(*) n from day_close where status='open' and business_date between :f and :l" + sub, **q)["n"]
        rs = one("select count(*) n, coalesce(sum(-amount_paise),0) v from recon_item where status='open' and source='settlement' and business_date between :f and :l" + sub, **q)
        rc = one("select count(*) n, coalesce(sum(-amount_paise),0) v from recon_item where status='open' and source='day_close' and business_date between :f and :l" + sub, **q)
        jb = one("select coalesce(sum(l.debit_paise),0) d, coalesce(sum(l.credit_paise),0) c from journal_line l join journal_entry j on j.id=l.jv_id where j.entity_id=:e and j.date between :f and :l", **q)
        prev = PERIODS[PERIODS.index(p) - 1] if p in PERIODS and PERIODS.index(p) > 0 else None
        pl = True if not prev else bool(one("select 1 x from period_lock where entity_id=:e and period=:p", e=e, p=prev))
        ch = [("ended", "Period has ended", last < END, f"Ends {last:%d %b %Y}"), ("prev", "Previous period is locked", pl, f"{prev} is not locked" if not pl else "Done"),
              ("dayclose", "Every store has closed its day", opn == 0, f"{opn} day closes are open" if opn else "All closed"),
              ("settle", "Settlement shortfalls are cleared", rs["n"] == 0, f"{rs['n']} items worth {money(rs['v'])} are open" if rs["n"] else "None open"),
              ("cash", "Cash variances are explained", rc["n"] == 0, f"{rc['n']} items worth {money(rc['v'])} are open" if rc["n"] else "None open"),
              ("balance", "Journal debits equal credits", jb["d"] == jb["c"], "Balanced" if jb["d"] == jb["c"] else "Out of balance")]
        if entity(e)["state"]:
            g = next((r for r in gst_rows(e) if r["period"] == p), None)
            ok = bool(g and g["available"] and g["at_risk"] == 0 and g["unbooked_n"] == 0)
            why = "GSTR-2B is not available yet" if g and not g["available"] else f"{g['missing_n']} invoices missing in GSTR-2B, {g['mismatch_n']} with a value mismatch, {g['unbooked_n']} not booked ({money(g['at_risk'])} at risk)"
            ch.append(("gst", "Input credit matches GSTR-2B", ok, "Matched" if ok else why))
        return [dict(key=k, label=lb, ok=bool(o), detail=d) for k, lb, o, d in ch]

    def lock_row(e, p): return one("select locked_at from period_lock where entity_id=:e and period=:p", e=e, p=p)

    @app.get("/api/finance/close")
    def close_board(period: str, u=Depends(need(FIN))):
        period_bounds(period); out = []
        for e in many("select id, code, name, state from legal_entity order by id"):
            lk = lock_row(e["id"], period); ch = [] if lk else checklist(e["id"], period)
            out.append(dict(entity=e, locked=bool(lk), locked_at=lk["locked_at"] if lk else None, ready=bool(lk) or all(c["ok"] for c in ch), blockers=[c for c in ch if not c["ok"]]))
        return dict(period=period, rows=out)

    @app.get("/api/finance/close/checklist")
    def close_check(entity_id: int, period: str, u=Depends(need(FIN))):
        entity(entity_id); lk = lock_row(entity_id, period)
        return dict(locked=bool(lk), checks=checklist(entity_id, period))

    @app.post("/api/finance/close/lock")
    def lock(b: Lock, u=Depends(need(FIN))):
        e = entity(b.entity_id); period_bounds(b.period)
        if lock_row(b.entity_id, b.period): raise HTTPException(409, f"{e['code']} {b.period} is already locked")
        bad = [c for c in checklist(b.entity_id, b.period) if not c["ok"]]
        if bad: raise HTTPException(422, f"Cannot lock {e['code']} {b.period}. " + "; ".join(f"{c['label']}: {c['detail']}" for c in bad))
        with engine.begin() as c:
            c.execute(text("insert into period_lock(entity_id, period, locked_at, locked_by) values (:e,:p,:t,:u)"), dict(e=b.entity_id, p=b.period, t=NOW, u=u["id"]))
            audit(c, u, "period.lock", "period_lock", f"{b.entity_id}:{b.period}", None, dict(entity=e["code"], period=b.period))
        return dict(entity_id=b.entity_id, period=b.period, locked=True)

    # ---------- payables ----------
    def payables():
        rows = many("select i.id, i.inv_no, i.inv_date, i.due_date, i.amount_minor, i.currency, i.status, v.id vid, v.name vendor, coalesce(f.rate_to_inr, 1.0) fx from vendor_invoice i join vendor v on v.id=i.vendor_id "
                    "left join fx_rate f on f.currency=i.currency and substr(cast(f.month as text),1,7)=substr(cast(i.inv_date as text),1,7) where i.status!='paid'")
        bk = {k: dict(n=0, v=0) for k in ("not_due", "d1_30", "d31_60", "d61_90", "d90_plus")}; ven = {}; due14 = 0
        for r in rows:
            r["inr"] = int(round(r["amount_minor"] * r["fx"])); r["overdue_days"] = (END - date.fromisoformat(str(r["due_date"])[:10])).days; od = r["overdue_days"]
            k = "not_due" if od <= 0 else "d1_30" if od <= 30 else "d31_60" if od <= 60 else "d61_90" if od <= 90 else "d90_plus"
            bk[k]["n"] += 1; bk[k]["v"] += r["inr"]; x = ven.setdefault(r["vid"], dict(vendor=r["vendor"], n=0, total=0, overdue=0)); x["n"] += 1; x["total"] += r["inr"]; x["overdue"] += r["inr"] if od > 0 else 0
            if -14 <= od <= 0: due14 += r["inr"]
        return dict(buckets=bk, total=sum(b["v"] for b in bk.values()), overdue=sum(b["v"] for k, b in bk.items() if k != "not_due"), due_next_14=due14,
                    vendors=sorted(ven.values(), key=lambda x: -x["total"])[:12], overdue_list=sorted([r for r in rows if r["overdue_days"] > 0], key=lambda r: -r["inr"])[:15])

    @app.get("/api/finance/payables")
    def payables_ep(u=Depends(need(PAY_VIEW))): return payables()

    # ---------- journal ----------
    def pb(period, entity_id):
        if period: first, last = period_bounds(period)
        else: first, last = date(2000, 1, 1), date(2100, 1, 1)
        entity(entity_id); return dict(e=entity_id, f=str(first), l=str(last))

    @app.get("/api/finance/trial-balance")
    def trial_balance(entity_id: int, period: str = "", u=Depends(need(FIN))):
        q = pb(period, entity_id)
        rows = many("select a.code, a.name, a.type, coalesce(sum(x.debit_paise),0) dr, coalesce(sum(x.credit_paise),0) cr from account a left join (select l.account_code, l.debit_paise, l.credit_paise from journal_line l join journal_entry j on j.id=l.jv_id "
                    "where j.entity_id=:e and j.date between :f and :l) x on x.account_code=a.code group by a.code, a.name, a.type order by a.code", **q)
        return dict(rows=rows, debit=sum(r["dr"] for r in rows), credit=sum(r["cr"] for r in rows))

    @app.get("/api/finance/journal/export")
    def export(entity_id: int, period: str = "", u=Depends(need(FIN))):
        q = pb(period, entity_id)
        rows = many("select j.jv_no, j.date, l.account_code, a.name, l.debit_paise, l.credit_paise, j.narration, j.source_doc_type from journal_entry j join journal_line l on l.jv_id=j.id join account a on a.code=l.account_code "
                    "where j.entity_id=:e and j.date between :f and :l order by j.date, j.id, l.id", **q)
        buf = io.StringIO(); w = csv.writer(buf); w.writerow(["voucher_no", "date", "account_code", "account_name", "debit", "credit", "narration", "source"])
        for r in rows: w.writerow([r["jv_no"], r["date"], r["account_code"], r["name"], f"{r['debit_paise'] / 100:.2f}", f"{r['credit_paise'] / 100:.2f}", r["narration"], r["source_doc_type"]])
        return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f"attachment; filename=journal-{entity(entity_id)['code']}-{period or 'all'}.csv"})

    @app.get("/api/finance/journal")
    def journal(entity_id: int, period: str = "", source: str = "", page: int = 1, size: int = 15, u=Depends(need(FIN))):
        q = pb(period, entity_id); w = " and j.source_doc_type=:s" if source else ""; q["s"] = source
        total = one("select count(*) n from journal_entry j where j.entity_id=:e and j.date between :f and :l" + w, **q)["n"]
        items = many("select j.id, j.jv_no, j.date, j.source_doc_type, j.narration, sum(l.debit_paise) amount, count(*) lines from journal_entry j join journal_line l on l.jv_id=j.id where j.entity_id=:e and j.date between :f and :l" + w +
                     " group by j.id, j.jv_no, j.date, j.source_doc_type, j.narration order by j.date desc, j.id desc limit :n offset :o", n=size, o=(max(page, 1) - 1) * size, **q)
        return dict(total=total, items=items, page=page, size=size)

    @app.get("/api/finance/journal/{jid}")
    def journal_one(jid: int, u=Depends(need(FIN))):
        j = one("select id, jv_no, date, source_doc_type, narration from journal_entry where id=:i", i=jid)
        if not j: raise HTTPException(404, "Journal not found")
        j["lines"] = many("select l.account_code, a.name, l.debit_paise, l.credit_paise from journal_line l join account a on a.code=l.account_code where l.jv_id=:i order by l.id", i=jid); return j

    # ---------- control tower ----------
    def exceptions(f):
        out = []
        if f["store_ids"] is not None: return out
        for r in [x for x in cash_rows(60) if x["flag"]][:3]:
            out.append(dict(type="cash", severity="high", title=f"{r['name']} is short on cash most weekends", detail=f"{r['city']}. {r['weekend_short_days']} weekend days short by Rs 1,000 or more in 60 days, {money(r['short_total'])} in total. One off shortages elsewhere are not flagged.", ref=r["id"]))
        for e in many("select id, code, name from legal_entity where state is not null order by id"):
            for g in gst_rows(e["id"]):
                if g["at_risk"] > 0 and not g["locked"]:
                    out.append(dict(type="gst", severity="high", title=f"{money(g['at_risk'])} of input credit at risk in {e['code']} for {g['period']}", detail=f"{g['missing_n']} invoices are missing in GSTR-2B and {g['mismatch_n']} have a different tax value. {g['unbooked_n']} portal lines are not booked. Filing is on hold until this is cleared.", ref=e["id"]))
        r = one("select count(*) n, coalesce(sum(-amount_paise),0) v from recon_item where status='open' and source='settlement' and business_date < :d", d=str(END - timedelta(days=7)))
        if r["n"]: out.append(dict(type="settlement", severity="med", title=f"{r['n']} gateway shortfalls are older than 7 days", detail=f"{money(r['v'])} has not reached the bank. Match or write it off in Finance.", ref=None))
        p = payables()
        if p["overdue"]: out.append(dict(type="payables", severity="med", title=f"{money(p['overdue'])} of vendor invoices are past due", detail=f"Total open payables are {money(p['total'])}. {money(p['due_next_14'])} falls due in the next 14 days.", ref=None))
        return out

    return dict(exceptions=exceptions)
