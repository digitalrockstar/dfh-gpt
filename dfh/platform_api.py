"""Platform operations API: access administration, approvals, audit, imports, sync and entities."""
from datetime import date, datetime, timedelta
from typing import Optional
from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

ADMINS = {"ceo", "admin"}
READERS = {"ceo", "admin", "finance", "procurement", "hr"}
DOC_TYPES = {"purchase_order", "vendor_invoice", "stock_adjustment", "wastage", "campaign", "loyalty_adjustment", "manual"}
SCOPE_TYPES = {"all", "store", "warehouse"}


class UserPatch(BaseModel):
    role_id: Optional[int] = None
    scope_type: Optional[str] = None
    scope_id: Optional[int] = None
    active: Optional[bool] = None


class RuleIn(BaseModel):
    doc_type: str
    min_amount_paise: int = Field(ge=0)
    max_amount_paise: Optional[int] = Field(default=None, ge=0)
    approver_role_id: int


class ApprovalIn(BaseModel):
    doc_type: str
    doc_id: str = Field(min_length=1, max_length=40)
    amount_paise: int = Field(ge=0)
    note: Optional[str] = None


class DecisionIn(BaseModel):
    decision: str
    note: Optional[str] = None


class FxIn(BaseModel):
    currency: str = Field(min_length=3, max_length=3)
    month: date
    rate_to_inr: float = Field(gt=0, le=100000)
    assumed: bool = True


def attach(app, ctx):
    me, need, many, one, audit, engine, NOW = (ctx[k] for k in ("me", "need", "many", "one", "audit", "engine", "NOW"))
    admin = need(ADMINS)
    reader = need(READERS)
    today = NOW.date()

    def serial(row):
        return {k: (v.isoformat() if isinstance(v, (date, datetime)) else v) for k, v in row.items()}

    def pager(page, size):
        return max(1, page), max(5, min(size, 100))

    def rule_for(doc_type, amount):
        return one(
            "select ar.id, ar.doc_type, ar.min_amount_paise, ar.max_amount_paise, ar.approver_role_id, r.code approver_role "
            "from approval_rule ar join role r on r.id=ar.approver_role_id "
            "where ar.doc_type=:d and ar.min_amount_paise<=:a and (ar.max_amount_paise is null or ar.max_amount_paise>=:a) "
            "order by ar.min_amount_paise desc limit 1", d=doc_type, a=amount)

    @app.get("/api/platform/options")
    def options(u=Depends(reader)):
        return dict(
            role=u["role"], can_admin=u["role"] in ADMINS,
            roles=many("select id,code,name from role order by id"),
            locations=many("select id,code,name,type,city from location where type in ('store','warehouse') order by type,city,name"),
            entities=many("select id,code,name,country,state,currency,gstin,is_demo from legal_entity order by id"),
            currencies=many("select distinct currency from fx_rate order by currency"),
            doc_types=sorted(DOC_TYPES), scope_types=sorted(SCOPE_TYPES), as_of=str(today))

    @app.get("/api/platform/summary")
    def summary(u=Depends(reader)):
        return dict(
            users=one("select count(*) n, sum(case when active then 1 else 0 end) active from app_user"),
            pending_approvals=one("select count(*) n, coalesce(sum(amount_paise),0) amount from approval_request where status='pending'"),
            audit_today=one("select count(*) n from audit_log where ts>=:s", s=str(datetime.combine(today, datetime.min.time()))),
            imports=one("select count(*) n, sum(case when status in ('queued','running','failed') then 1 else 0 end) attention from import_job"),
            devices=one("select count(*) n, sum(case when active then 1 else 0 end) active from device"),
            stale_devices=one("select count(*) n from device where active=true and (last_seen_at is null or last_seen_at<:cut)", cut=str(NOW-timedelta(hours=24))),
            entities=one("select count(*) n from legal_entity"),
            latest_audit=many("select a.id,a.ts,a.action,a.table_name,a.row_id,u.username,u.name from audit_log a left join app_user u on u.id=a.user_id order by a.ts desc,a.id desc limit 8"))

    @app.get("/api/platform/users")
    def users(q: str = "", role: str = "", active: str = "", page: int = 1, size: int = 30, u=Depends(reader)):
        page, size = pager(page, size)
        where, p = ["1=1"], {}
        if q:
            where.append("(lower(u.name) like :q or lower(u.username) like :q)")
            p["q"] = "%" + q.lower() + "%"
        if role:
            where.append("r.code=:role"); p["role"] = role
        if active in ("0", "1"):
            where.append("u.active=:active"); p["active"] = active == "1"
        clause = " where " + " and ".join(where)
        total = one("select count(*) n from app_user u join role r on r.id=u.role_id" + clause, **p)["n"]
        items = many("select u.id,u.username,u.name,u.active,u.scope_type,u.scope_id,u.last_login,r.id role_id,r.code role,r.name role_name,l.name scope_name "
                     "from app_user u join role r on r.id=u.role_id left join location l on l.id=u.scope_id" + clause +
                     " order by u.username limit :lim offset :off", **p, lim=size, off=(page-1)*size)
        return dict(items=items,total=total,page=page,size=size)

    @app.patch("/api/platform/users/{uid}")
    def update_user(uid: int, b: UserPatch, u=Depends(admin)):
        target = one("select u.*,r.code role from app_user u join role r on r.id=u.role_id where u.id=:i", i=uid)
        if not target:
            raise HTTPException(404, "User not found")
        changes = b.dict(exclude_unset=True)
        if not changes:
            raise HTTPException(422, "No changes supplied")
        if uid == u["id"]:
            if changes.get("active") is False or changes.get("role_id", target["role_id"]) != target["role_id"] or changes.get("scope_type", target["scope_type"]) != target["scope_type"] or changes.get("scope_id", target["scope_id"]) != target["scope_id"]:
                raise HTTPException(409, "You cannot change your own access")
        if "role_id" in changes and not one("select id from role where id=:i", i=changes["role_id"]):
            raise HTTPException(422, "Unknown role")
        scope_type = changes.get("scope_type", target["scope_type"])
        scope_id = changes.get("scope_id", target["scope_id"])
        if scope_type not in SCOPE_TYPES:
            raise HTTPException(422, "Unknown scope type")
        if scope_type == "all":
            if scope_id is not None:
                raise HTTPException(422, "All-location scope cannot have a location")
            changes["scope_id"] = None
        else:
            if scope_id is None:
                raise HTTPException(422, "Choose a location for this scope")
            loc = one("select id,type from location where id=:i and active=true", i=scope_id)
            if not loc or loc["type"] != scope_type:
                raise HTTPException(422, "Location does not match the selected scope")
        allowed = {"role_id", "scope_type", "scope_id", "active"}
        if set(changes)-allowed:
            raise HTTPException(422, "Unsupported user field")
        with engine.begin() as c:
            before = {k: target[k] for k in changes}
            c.execute(__import__("sqlalchemy").text("update app_user set " + ", ".join(k+"=:"+k for k in changes) + " where id=:id"), {**changes, "id":uid})
            audit(c, u, "update", "app_user", uid, before, changes)
        return dict(id=uid, updated=True)

    @app.get("/api/platform/approval-rules")
    def approval_rules(u=Depends(reader)):
        return many("select ar.id,ar.doc_type,ar.min_amount_paise,ar.max_amount_paise,ar.approver_role_id,r.code approver_role,r.name approver_name "
                    "from approval_rule ar join role r on r.id=ar.approver_role_id order by ar.doc_type,ar.min_amount_paise")

    @app.post("/api/platform/approval-rules")
    def create_rule(b: RuleIn, u=Depends(admin)):
        if b.doc_type not in DOC_TYPES:
            raise HTTPException(422, "Unknown document type")
        if b.max_amount_paise is not None and b.max_amount_paise < b.min_amount_paise:
            raise HTTPException(422, "Maximum amount must be at least the minimum")
        role = one("select id from role where id=:i", i=b.approver_role_id)
        if not role:
            raise HTTPException(422, "Unknown approver role")
        overlap = one("select id from approval_rule where doc_type=:d and min_amount_paise<=:hi and (max_amount_paise is null or max_amount_paise>=:lo)",
                      d=b.doc_type, hi=b.max_amount_paise if b.max_amount_paise is not None else 9223372036854775807, lo=b.min_amount_paise)
        if overlap:
            raise HTTPException(409, "This amount range overlaps an existing rule")
        with engine.begin() as c:
            rid = c.execute(__import__("sqlalchemy").text("insert into approval_rule(doc_type,min_amount_paise,max_amount_paise,approver_role_id) values (:d,:lo,:hi,:r) returning id"),
                            dict(d=b.doc_type,lo=b.min_amount_paise,hi=b.max_amount_paise,r=b.approver_role_id)).scalar()
            audit(c,u,"create","approval_rule",rid,None,b.dict())
        return dict(id=rid)

    @app.delete("/api/platform/approval-rules/{rid}")
    def delete_rule(rid: int, u=Depends(admin)):
        old = one("select * from approval_rule where id=:i", i=rid)
        if not old:
            raise HTTPException(404, "Rule not found")
        with engine.begin() as c:
            c.execute(__import__("sqlalchemy").text("delete from approval_rule where id=:i"), {"i":rid})
            audit(c,u,"delete","approval_rule",rid,old,None)
        return dict(id=rid,deleted=True)

    @app.get("/api/platform/approvals")
    def approvals(status: str = "pending", page: int = 1, size: int = 30, u=Depends(reader)):
        page,size=pager(page,size)
        where = " where 1=1" + (" and a.status=:s" if status else "")
        p={"s":status} if status else {}
        total=one("select count(*) n from approval_request a"+where,**p)["n"]
        items=many("select a.id,a.doc_type,a.doc_id,a.status,a.amount_paise,a.created_at,a.decided_at,a.note,req.username requested_by,ap.username approver,rr.code approver_role "
                   "from approval_request a left join app_user req on req.id=a.requested_by left join app_user ap on ap.id=a.approver_id "
                   "left join approval_rule ar on ar.doc_type=a.doc_type and ar.min_amount_paise<=a.amount_paise and (ar.max_amount_paise is null or ar.max_amount_paise>=a.amount_paise) "
                   "left join role rr on rr.id=ar.approver_role_id"+where+" order by a.created_at desc,a.id desc limit :lim offset :off",
                   **p,lim=size,off=(page-1)*size)
        for x in items:
            x["can_decide"] = u["role"] in ADMINS or x["approver_role"] == u["role"]
            x["is_self"] = False
        return dict(items=items,total=total,page=page,size=size)

    @app.post("/api/platform/approvals")
    def request_approval(b: ApprovalIn, u=Depends(me)):
        if b.doc_type not in DOC_TYPES:
            raise HTTPException(422, "Unknown document type")
        rule=rule_for(b.doc_type,b.amount_paise)
        if not rule:
            raise HTTPException(422, "No approval rule covers this amount")
        with engine.begin() as c:
            rid=c.execute(__import__("sqlalchemy").text("insert into approval_request(doc_type,doc_id,requested_by,status,amount_paise,created_at,note) values (:d,:ref,:uid,'pending',:a,:t,:n) returning id"),
                          dict(d=b.doc_type,ref=b.doc_id,uid=u["id"],a=b.amount_paise,t=NOW,n=b.note)).scalar()
            audit(c,u,"request","approval_request",rid,None,b.dict())
        return dict(id=rid,status="pending",approver_role=rule["approver_role"])

    @app.post("/api/platform/approvals/{aid}/decision")
    def decide_approval(aid: int,b: DecisionIn,u=Depends(me)):
        if b.decision not in {"approve","reject"}:
            raise HTTPException(422,"Decision must be approve or reject")
        req=one("select * from approval_request where id=:i",i=aid)
        if not req:
            raise HTTPException(404,"Approval request not found")
        if req["status"]!="pending":
            raise HTTPException(409,"This request has already been decided")
        rule=rule_for(req["doc_type"],req["amount_paise"])
        if not rule or (u["role"] not in ADMINS and u["role"]!=rule["approver_role"]):
            raise HTTPException(403,"Your role cannot decide this request")
        if req["requested_by"]==u["id"]:
            raise HTTPException(409,"A request cannot be approved by its requester")
        if b.decision=="reject" and not (b.note or "").strip():
            raise HTTPException(422,"A reason is required when rejecting")
        with engine.begin() as c:
            c.execute(__import__("sqlalchemy").text("update approval_request set status=:s,approver_id=:u,decided_at=:t,note=:n where id=:i and status='pending'"),
                      dict(s="approved" if b.decision=="approve" else "rejected",u=u["id"],t=NOW,n=b.note,i=aid))
            audit(c,u,b.decision,"approval_request",aid,{"status":"pending"},{"status":"approved" if b.decision=="approve" else "rejected","note":b.note})
        return dict(id=aid,status="approved" if b.decision=="approve" else "rejected")

    @app.get("/api/platform/audit")
    def audit_rows(q: str = "", action: str = "", table_name: str = "", user_id: int = 0,
                   from_date: date = None, to_date: date = None, page: int = 1, size: int = 50, u=Depends(reader)):
        page,size=pager(page,size)
        where,p=["1=1"],{}
        if q:
            where.append("(lower(a.row_id) like :q or lower(a.table_name) like :q or lower(coalesce(a.action,'')) like :q)")
            p["q"]="%"+q.lower()+"%"
        if action: where.append("a.action=:action");p["action"]=action
        if table_name: where.append("a.table_name=:tb");p["tb"]=table_name
        if user_id: where.append("a.user_id=:uid");p["uid"]=user_id
        if from_date: where.append("a.ts>=:start");p["start"]=str(datetime.combine(from_date,datetime.min.time()))
        if to_date: where.append("a.ts<:end");p["end"]=str(datetime.combine(to_date+timedelta(days=1),datetime.min.time()))
        clause=" where "+" and ".join(where)
        total=one("select count(*) n from audit_log a"+clause,**p)["n"]
        items=many("select a.id,a.ts,a.user_id,a.action,a.table_name,a.row_id,a.before,a.after,u.username,u.name from audit_log a left join app_user u on u.id=a.user_id"+clause+
                   " order by a.ts desc,a.id desc limit :lim offset :off",**p,lim=size,off=(page-1)*size)
        return dict(items=items,total=total,page=page,size=size)

    @app.get("/api/platform/imports")
    def imports(status: str = "", page: int = 1, size: int = 30, u=Depends(reader)):
        page,size=pager(page,size)
        clause=" where status=:s" if status else ""
        p={"s":status}
        total=one("select count(*) n from import_job"+clause,**p)["n"]
        items=many("select id,kind,filename,status,rows_ok,rows_err,created_at,report from import_job"+clause+
                   " order by created_at desc,id desc limit :lim offset :off",**p,lim=size,off=(page-1)*size)
        return dict(items=items,total=total,page=page,size=size)

    @app.get("/api/platform/sync")
    def sync(u=Depends(reader)):
        devices=many("select d.id,d.code,d.kind,d.store_id,l.name store,l.city,d.last_seen_at,d.app_version,d.active,"
                     "max(s.started_at) last_sync,max(s.queue_depth) queue_depth,max(s.bills_rejected) bills_rejected "
                     "from device d left join location l on l.id=d.store_id left join sync_log s on s.device_id=d.id group by d.id,d.code,d.kind,d.store_id,l.name,l.city,d.last_seen_at,d.app_version,d.active order by d.last_seen_at")
        for d in devices:
            last=d["last_seen_at"]
            if last:
                dt=last if isinstance(last,datetime) else datetime.fromisoformat(str(last))
                d["age_hours"]=round((NOW-dt).total_seconds()/3600,1)
            else:d["age_hours"]=None
            d["health"]="never" if not last else "stale" if d["age_hours"]>=24 else "late" if d["age_hours"]>=1 else "ok"
        return dict(devices=devices,imports=many("select status,count(*) n from import_job group by status order by status"))

    @app.get("/api/platform/entities")
    def entities(u=Depends(reader)):
        items=many("select e.id,e.code,e.name,e.country,e.state,e.currency,e.gstin,e.is_demo,count(distinct f.id) fx_months "
                   "from legal_entity e left join fx_rate f on f.currency=e.currency group by e.id,e.code,e.name,e.country,e.state,e.currency,e.gstin,e.is_demo order by e.id")
        currencies=many("select currency,month,rate_to_inr,assumed from fx_rate order by month desc,currency")
        return dict(items=items,fx=currencies)

    @app.post("/api/platform/fx")
    def add_fx(b: FxIn,u=Depends(admin)):
        if b.month.day!=1:
            raise HTTPException(422,"FX month must be the first day of a month")
        old=one("select id,rate_to_inr,assumed from fx_rate where currency=:c and month=:m",c=b.currency.upper(),m=b.month)
        values=dict(currency=b.currency.upper(),month=b.month.isoformat(),rate_to_inr=b.rate_to_inr,assumed=b.assumed)
        with engine.begin() as c:
            if old:
                c.execute(__import__("sqlalchemy").text("update fx_rate set rate_to_inr=:r,assumed=:a where id=:i"),dict(r=b.rate_to_inr,a=b.assumed,i=old["id"]))
                audit(c,u,"update","fx_rate",old["id"],old,values)
                rid=old["id"]
            else:
                rid=c.execute(__import__("sqlalchemy").text("insert into fx_rate(currency,month,rate_to_inr,assumed) values (:currency,:month,:rate_to_inr,:assumed) returning id"),values).scalar()
                audit(c,u,"create","fx_rate",rid,None,values)
        return dict(id=rid,updated=bool(old))
