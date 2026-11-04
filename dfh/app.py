import csv, io, json, os
from datetime import datetime, date
from typing import Optional
from fastapi import FastAPI, Depends, HTTPException, Header, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import text
from . import auth
from .clock import current_time
from .analytics import Analytics
from .db import get_engine
from . import wastage_api, demand_api, procurement_api, pos_api, finance_api, ct_api, crm_api

STATUSES = ["draft", "active", "restricted", "delisted"]
MOVES = {"draft": {"active", "delisted"}, "active": {"restricted", "delisted"}, "restricted": {"active", "delisted"}, "delisted": {"active"}}
CT_ROLES = {"ceo", "finance", "admin", "procurement", "warehouse", "store_manager", "marketing"}
SKU_WRITE = {"ceo", "procurement", "admin"}
SORTS = {"name": "s.name", "code": "s.code", "price": "price", "cost": "s.std_cost_paise", "margin": "gm", "popularity": "s.popularity"}
PRICE = "coalesce(s.mrp_paise, s.price_per_kg_paise)"
GM = f"(1.0 - s.std_cost_paise / ({PRICE} / (1 + coalesce(t.rate_packed_pct, 5.0) / 100.0)))"
SKU_SELECT = (f"select s.id, s.code, s.barcode, s.name, s.grade, s.pack_g, s.base_uom, s.procurement_type, s.status, s.hsn, s.mrp_paise, s.price_per_kg_paise, "
              f"s.std_cost_paise, s.shelf_life_days, s.bulk_sku_id, s.product_id, c.code cat_code, c.name cat_name, p.is_fresh, p.origins, {PRICE} price, {GM} gm, "
              "coalesce(t.rate_packed_pct, 5.0) tax_pct, t.assumed tax_assumed from sku s join product p on p.id=s.product_id join category c on c.id=p.category_id "
              "left join tax_rate t on t.hsn=s.hsn")


class LoginIn(BaseModel):
    username: str
    password: str


class SkuPatch(BaseModel):
    status: Optional[str] = None
    mrp_paise: Optional[int] = None
    price_per_kg_paise: Optional[int] = None
    reason: Optional[str] = None


class ImportIn(BaseModel):
    csv: str
    apply: bool = False


def create_app(db_url=None, data_dir=None, serve_frontend=True):
    now = current_time()
    engine = get_engine(db_url)
    data_dir = data_dir or os.getenv("DFH_DATA", "data")
    an = Analytics(engine, data_dir)
    app = FastAPI(title="DFH Operations Platform (demo)")
    rows = lambda r: [dict(x._mapping) for x in r]

    def one(sql, **p):
        with engine.connect() as c:
            r = c.execute(text(sql), p).fetchone()
        return dict(r._mapping) if r else None

    def many(sql, **p):
        with engine.connect() as c:
            return rows(c.execute(text(sql), p).fetchall())

    def me(authorization: str = Header(default="")):
        uid = auth.read_token(authorization.replace("Bearer ", ""))
        u = one("select u.id, u.username, u.name, r.code role, u.scope_type, u.scope_id from app_user u join role r on r.id=u.role_id where u.id=:i and u.active=1", i=uid) if uid else None
        if not u: raise HTTPException(401, "Not signed in")
        return u

    def need(roles):
        def dep(u=Depends(me)):
            if u["role"] not in roles: raise HTTPException(403, "Your role cannot do this")
            return u
        return dep

    def scope(u):
        if u["scope_type"] == "store": return [u["scope_id"]]
        if u["scope_type"] == "warehouse": return [r["id"] for r in many("select id from location where serving_wh_id=:w", w=u["scope_id"])]
        return None

    def audit(c, u, action, table, row_id, before, after):
        c.execute(text("insert into audit_log(ts, user_id, action, table_name, row_id, before, after) values (:t,:u,:a,:tb,:r,:b,:af)"),
                  dict(t=now, u=u["id"], a=action, tb=table, r=str(row_id), b=json.dumps(before), af=json.dumps(after)))

    @app.get("/api/health")
    def health(): return dict(ok=True, data_through=str(an.max_date))

    @app.post("/api/login")
    def login(b: LoginIn):
        u = one("select u.id, u.name, u.password_hash, r.code role from app_user u join role r on r.id=u.role_id where u.username=:n and u.active=1", n=b.username.strip().lower())
        if not u or not auth.verify_pw(b.password, u["password_hash"]): raise HTTPException(401, "Wrong username or password")
        if auth.needs_rehash(u["password_hash"]):
            with engine.begin() as c:
                c.execute(text("update app_user set password_hash=:h where id=:i"), {"h": auth.hash_pw(b.password), "i": u["id"]})
        return dict(token=auth.make_token(u["id"]), name=u["name"], role=u["role"])

    @app.get("/api/me")
    def whoami(u=Depends(me)): return u

    # ---------------- SKU master ----------------
    @app.get("/api/categories")
    def categories(u=Depends(me)):
        return many("select c.code, c.name, c.site_collection, count(s.id) skus, sum(case when s.status='active' then 1 else 0 end) active from category c "
                    "left join product p on p.category_id=c.id left join sku s on s.product_id=p.id group by c.id order by c.id")

    @app.get("/api/skus")
    def skus(q: str = "", cat: str = "", ptype: str = "", status: str = "", sort: str = "popularity", dir: str = "desc", page: int = 1, size: int = 50, u=Depends(me)):
        w, p = [], {}
        if q: w.append("(lower(s.name) like :q or lower(s.code) like :q or s.barcode like :q)"); p["q"] = f"%{q.lower()}%"
        if cat: w.append("c.code=:cat"); p["cat"] = cat
        if ptype: w.append("s.procurement_type=:pt"); p["pt"] = ptype
        if status: w.append("s.status=:st"); p["st"] = status
        wh = (" where " + " and ".join(w)) if w else ""
        order = SORTS.get(sort, "s.popularity") + (" asc" if dir == "asc" else " desc")
        size = max(1, min(size, 200))
        total = one("select count(*) n from sku s join product p on p.id=s.product_id join category c on c.id=p.category_id" + wh, **p)["n"]
        items = many(f"{SKU_SELECT}{wh} order by {order}, s.id limit :lim offset :off", **p, lim=size, off=(max(1, page) - 1) * size)
        return dict(total=total, page=page, size=size, items=items)

    @app.get("/api/skus/duplicates")
    def duplicates(u=Depends(me)):
        return many("select lower(name) name, count(*) n from sku group by 1 having count(*)>1")

    @app.get("/api/skus/{sid}")
    def sku_detail(sid: int, u=Depends(me)):
        s = one(SKU_SELECT + " where s.id=:i", i=sid)
        if not s: raise HTTPException(404, "SKU not found")
        src = s["bulk_sku_id"] or s["id"]
        s["bulk_parent"] = one("select id, code, name from sku where id=:i", i=s["bulk_sku_id"]) if s["bulk_sku_id"] else None
        s["packs"] = many("select id, code, name, pack_g, mrp_paise, status from sku where bulk_sku_id=:i order by pack_g", i=s["id"])
        comps = many("select k.qty, c.id, c.code, c.name, c.pack_g, c.std_cost_paise, c.mrp_paise from kit_bom k join sku c on c.id=k.component_sku_id where k.kit_sku_id=:i", i=sid)
        s["kit"] = dict(components=comps, component_cost=sum(c["std_cost_paise"] * c["qty"] for c in comps)) if comps else None
        vend = many("select v.id vendor_id, v.name, v.country, v.type, v.is_trader, c.currency, c.price_per, c.incoterm, c.moq_qty, c.lead_time_days, c.effective_from, c.price_minor, "
                    "coalesce(f.rate_to_inr, 1.0) fx from vendor_sku_cost c join vendor v on v.id=c.vendor_id left join fx_rate f on f.currency=c.currency and f.month=c.effective_from "
                    "where c.sku_id=:i order by v.id, c.effective_from", i=src)
        book = {}
        for r in vend:
            b = book.setdefault(r["vendor_id"], dict(vendor_id=r["vendor_id"], name=r["name"], country=r["country"], type=r["type"], trader=bool(r["is_trader"]), currency=r["currency"],
                                                    unit=r["price_per"], incoterm=r["incoterm"], moq=r["moq_qty"], lead_days=r["lead_time_days"], series=[]))
            b["series"].append(dict(month=str(r["effective_from"])[:7], inr=round(r["price_minor"] / 100 * r["fx"], 2)))
        for b in book.values():
            sr = b["series"]; b["latest"] = sr[-1]["inr"]; b["chg_3m"] = (sr[-1]["inr"] / sr[-4]["inr"] - 1) if len(sr) >= 4 else None
        s["vendors"] = sorted(book.values(), key=lambda b: b["latest"])
        s["vendor_source_note"] = "Vendor prices are for the bulk parent SKU" if s["bulk_sku_id"] else ("Vendor prices per unit" if s["procurement_type"] == "finished" else None)
        s["history"] = many("select a.ts, a.action, a.before, a.after, u.name by_user from audit_log a left join app_user u on u.id=a.user_id where a.table_name='sku' and a.row_id=:r order by a.id desc limit 15", r=str(sid))
        s["sales30"] = an.sku_sales(sid, 30)
        return s

    def _validate_patch(s, b, role):
        if role not in SKU_WRITE: raise HTTPException(403, "Your role cannot change SKUs")
        ch = {}
        if b.status is not None:
            if b.status not in STATUSES: raise HTTPException(422, "Unknown status")
            if b.status != s["status"] and b.status not in MOVES[s["status"]]: raise HTTPException(422, f"Cannot move {s['status']} to {b.status}")
            ch["status"] = b.status
        for fld, ok in (("mrp_paise", s["base_uom"] == "ea"), ("price_per_kg_paise", s["base_uom"] == "g")):
            v = getattr(b, fld)
            if v is None: continue
            if not ok: raise HTTPException(422, f"{fld} does not apply to this SKU")
            if v <= 0: raise HTTPException(422, "Price must be positive")
            if v / (1 + s["tax_pct"] / 100) <= s["std_cost_paise"]: raise HTTPException(422, "Price is at or below cost after tax")
            ch[fld] = v
        if not ch: raise HTTPException(422, "Nothing to change")
        return ch

    @app.patch("/api/skus/{sid}")
    def sku_patch(sid: int, b: SkuPatch, u=Depends(me)):
        s = one(SKU_SELECT + " where s.id=:i", i=sid)
        if not s: raise HTTPException(404, "SKU not found")
        ch = _validate_patch(s, b, u["role"])
        with engine.begin() as c:
            c.execute(text("update sku set " + ", ".join(f"{k}=:{k}" for k in ch) + " where id=:i"), dict(ch, i=sid))
            audit(c, u, "price_change" if len(ch) and "status" not in ch else "update", "sku", sid, {k: s[k] for k in ch}, dict(ch, reason=b.reason))
        return one(SKU_SELECT + " where s.id=:i", i=sid)

    @app.post("/api/skus/import")
    def sku_import(b: ImportIn, u=Depends(need(SKU_WRITE))):
        rd = list(csv.DictReader(io.StringIO(b.csv.strip())))
        errs, plan = [], []
        for i, r in enumerate(rd, start=2):
            code = (r.get("sku_code") or "").strip()
            s = one(SKU_SELECT + " where s.code=:c", c=code)
            if not s: errs.append(dict(row=i, code=code, error="Unknown SKU code")); continue
            try:
                patch = SkuPatch(status=(r.get("status") or "").strip() or None,
                                 mrp_paise=int(round(float(r["mrp_rupees"]) * 100)) if (r.get("mrp_rupees") or "").strip() else None,
                                 price_per_kg_paise=int(round(float(r["price_per_kg_rupees"]) * 100)) if (r.get("price_per_kg_rupees") or "").strip() else None)
                plan.append((s, _validate_patch(s, patch, u["role"]), patch))
            except HTTPException as e: errs.append(dict(row=i, code=code, error=e.detail))
            except ValueError: errs.append(dict(row=i, code=code, error="Not a number"))
        applied = False
        if b.apply and not errs and plan:
            with engine.begin() as c:
                for s, ch, patch in plan:
                    c.execute(text("update sku set " + ", ".join(f"{k}=:{k}" for k in ch) + " where id=:i"), dict(ch, i=s["id"]))
                    audit(c, u, "import", "sku", s["id"], {k: s[k] for k in ch}, ch)
                c.execute(text("insert into import_job(kind, filename, status, rows_ok, rows_err, created_at) values ('sku_prices','upload','applied',:o,0,:t)"), dict(o=len(plan), t=now))
            applied = True
        return dict(rows=len(rd), ok=len(plan), errors=errs, applied=applied)

    # ---------------- Control Tower v0 ----------------
    def filters(u, region, tier, city, fmt=""):
        return dict(region=region or None, tier=tier or None, city=city or None, fmt=fmt or None, store_ids=scope(u))

    wa = wastage_api.attach(app, dict(me=me, need=need, many=many, one=one, audit=audit, engine=engine, an=an, **{"NOW": now}))
    da = demand_api.attach(app, dict(me=me, need=need, many=many, one=one, audit=audit, engine=engine, an=an, **{"NOW": now}))
    pos_api.attach(app, dict(me=me, need=need, many=many, one=one, audit=audit, engine=engine, an=an, **{"NOW": now}))
    fa = finance_api.attach(app, dict(me=me, need=need, many=many, one=one, audit=audit, engine=engine, an=an, **{"NOW": now}))
    ca = crm_api.attach(app, dict(me=me, need=need, many=many, one=one, audit=audit, engine=engine, an=an, **{"NOW": now}))
    pa = procurement_api.attach(app, dict(me=me, need=need, many=many, one=one, audit=audit, engine=engine, an=an, **{"NOW": now}))
    ct = need(CT_ROLES)

    @app.get("/api/ct/summary")
    def ct_summary(days: int = 30, region: str = "", tier: str = "", city: str = "", fmt: str = "", u=Depends(ct)):
        return an.summary(days, **filters(u, region, tier, city, fmt))

    @app.get("/api/ct/trend")
    def ct_trend(days: int = 30, region: str = "", tier: str = "", city: str = "", fmt: str = "", u=Depends(ct)):
        return an.trend(days, **filters(u, region, tier, city, fmt))

    @app.get("/api/ct/categories")
    def ct_cats(days: int = 30, region: str = "", tier: str = "", city: str = "", fmt: str = "", u=Depends(ct)):
        return an.category_mix(days, **filters(u, region, tier, city, fmt))

    @app.get("/api/ct/league")
    def ct_league(days: int = 30, order: str = "top", region: str = "", tier: str = "", city: str = "", fmt: str = "", limit: int = 10, u=Depends(ct)):
        return an.league(days, order=order, limit=max(5, min(limit, 50)), **filters(u, region, tier, city, fmt))

    def exceptions_for(f):
        out = []
        for r in many("select id, name, city, last_sync_at from location where type='store' and last_sync_at is not null order by last_sync_at limit 400"):
            ls = r["last_sync_at"] if isinstance(r["last_sync_at"], datetime) else datetime.fromisoformat(str(r["last_sync_at"]))
            hrs = (now - ls).total_seconds() / 3600
            if hrs >= 24 and (f["store_ids"] is None or r["id"] in f["store_ids"]):
                out.append(dict(type="sync", severity="high", title=f"{r['name']} has not synced for {int(hrs // 24)} days", detail=f"{r['city']}. Last sync {ls:%d %b, %H:%M}. Bills may be queued on the device.", ref=r["id"]))
        for h in an.stockout_hotspots(30, **f):
            out.append(dict(type="stockout", severity="high", title=f"Stockout hotspot in {h['city']}", detail=f"{h['events']} stockout events in 30 days across {h['stores']} stores, {h['ratio']:.1f}x the typical city. Most hit: {', '.join(h['top_skus'])}.", ref=h["city"]))
        for d in an.sales_drops(**f):
            out.append(dict(type="drop", severity="med", title=f"{d['name']} is selling {int((1 - d['ratio']) * 100)}% below its recent run rate", detail=f"{d['city']}. Last 7 days against the previous 4 weeks.", ref=d["id"]))
        out += wa["exceptions"](f)
        out += da["exceptions"](f)
        out += pa["exceptions"](f)
        out += fa["exceptions"](f)
        out += ca["exceptions"](f)
        order = {"high": 0, "med": 1}
        return sorted(out, key=lambda x: order[x["severity"]])

    @app.get("/api/ct/exceptions")
    def ct_exc(region: str = "", tier: str = "", city: str = "", fmt: str = "", u=Depends(ct)):
        return exceptions_for(filters(u, region, tier, city, fmt))

    ct_api.attach(app, dict(me=me, need=need, many=many, one=one, audit=audit, engine=engine, an=an, **{"NOW": now}), exceptions_for, ct, filters)

    @app.get("/api/ct/filters")
    def ct_filters(u=Depends(ct)):
        return dict(formats=[r["format"] for r in many("select distinct format from location where type='store' and format is not null order by format")], regions=[r["region"] for r in many("select distinct region from location where type='store' order by region")], cities=[r["city"] for r in many("select distinct city from location where type='store' order by city")])

    from .inventory import register as register_inventory
    register_inventory(app, engine, one, many, me, need, audit, now)

    if serve_frontend:
        web = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")
        app.mount("/", StaticFiles(directory=web, html=True), name="web")
    return app


app = None


def get_app():
    global app
    if app is None: app = create_app()
    return app
