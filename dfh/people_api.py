"""People and HR API: employee directory, attendance, rosters, leave, onboarding and hiring."""
from datetime import date, datetime, timedelta
from typing import Optional
from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

READ = {"ceo", "admin", "hr", "store_manager", "warehouse"}
HR = {"ceo", "admin", "hr"}
MANAGERS = {"ceo", "admin", "hr", "store_manager", "warehouse"}
LEAVE_APPROVERS = {"ceo", "admin", "hr", "store_manager", "warehouse"}


class AttendanceIn(BaseModel):
    employee_id: int
    date: date
    status: str = "present"
    punch_in: Optional[datetime] = None
    punch_out: Optional[datetime] = None
    source: str = "manual"


class RosterIn(BaseModel):
    employee_id: int
    date: date
    shift_id: int
    location_id: int


class LeaveIn(BaseModel):
    employee_id: int
    leave_type_id: int
    from_date: date
    to_date: date
    is_wfh: bool = False


class DecisionIn(BaseModel):
    decision: str
    note: Optional[str] = None


class JobIn(BaseModel):
    title: str
    department_id: int
    location_id: Optional[int] = None
    posted_on: date
    closes_on: Optional[date] = None
    description: str = ""


class ApplicantStageIn(BaseModel):
    stage: str


def attach(app, ctx):
    me, need, many, one, audit, engine, NOW = (ctx[k] for k in ("me", "need", "many", "one", "audit", "engine", "NOW"))
    today = NOW.date()
    read = need(READ)
    hr = need(HR)
    manager = need(MANAGERS)
    stages = {"applied", "screening", "interview", "offer", "hired", "rejected"}
    attendance_statuses = {"present", "late", "half_day", "absent", "leave", "holiday"}

    def scoped(u, alias="e"):
        if u["role"] in HR or u["scope_type"] == "all":
            return "", {}
        if u["scope_type"] in ("store", "warehouse"):
            return f" and {alias}.location_id=:people_scope", {"people_scope": u["scope_id"]}
        return " and 1=0", {}

    def employee(eid, u):
        e = one("select e.*, d.name department, g.title designation, l.name location, l.type location_type from employee e join department d on d.id=e.department_id join designation g on g.id=e.designation_id join location l on l.id=e.location_id where e.id=:i", i=eid)
        extra, params = scoped(u)
        if not e or (extra and not many("select id from employee e where e.id=:i" + extra, i=eid, **params)):
            raise HTTPException(404, "Employee not found")
        return e

    def serial(values):
        return {k: (v.isoformat() if isinstance(v, (date, datetime)) else v) for k, v in values.items()}

    def paged(page, size):
        return max(1, page), max(5, min(size, 100))

    @app.get("/api/people/options")
    def options(u=Depends(read)):
        return dict(
            role=u["role"], can_manage=u["role"] in HR, can_approve=u["role"] in LEAVE_APPROVERS,
            departments=many("select id,name from department order by name"),
            designations=many("select id,title,level from designation order by level desc,title"),
            locations=many("select id,code,name,type,city from location where type in ('store','warehouse','office') order by type,city,name"),
            shifts=many("select id,code,start_time,end_time from shift order by start_time"),
            leave_types=many("select id,code,name,annual_quota from leave_type order by id"),
        )

    @app.get("/api/people/summary")
    def summary(u=Depends(read)):
        extra, params = scoped(u)
        base = " from employee e where e.status='active'" + extra
        staff = one("select count(*) n" + base, **params)["n"]
        today_att = one("select count(*) n, sum(case when status in ('present','late','half_day') then 1 else 0 end) present, sum(case when status='absent' then 1 else 0 end) absent from attendance a join employee e on e.id=a.employee_id where a.date=:d" + extra.replace("e.location_id", "e.location_id"), d=today, **params)
        pending = one("select count(*) n from leave_request r join employee e on e.id=r.employee_id where r.status='pending'" + extra, **params)["n"]
        onboarding = one("select count(*) n from onboarding_task t join employee e on e.id=t.employee_id where t.done_at is null" + extra, **params)["n"]
        jobs = one("select count(*) n from job_posting where status='open'")["n"]
        recent = many("select e.id,e.emp_code,e.name,d.name department,g.title designation,l.name location,e.join_date from employee e join department d on d.id=e.department_id join designation g on g.id=e.designation_id join location l on l.id=e.location_id where e.status='active'" + extra + " order by e.join_date desc,e.id desc limit 6", **params)
        return dict(active=staff, attendance=dict(present=int(today_att["present"] or 0), absent=int(today_att["absent"] or 0), records=int(today_att["n"] or 0)),
                    pending_leaves=pending, onboarding_open=onboarding, open_jobs=jobs, recent_joins=recent, as_of=str(today))

    @app.get("/api/people/employees")
    def employees(q: str = "", department_id: int = 0, location_id: int = 0, status: str = "active", page: int = 1, size: int = 30, u=Depends(read)):
        page, size = paged(page, size)
        extra, params = scoped(u)
        where = " where 1=1" + extra
        if q:
            where += " and (lower(e.name) like :q or lower(e.emp_code) like :q or lower(coalesce(e.email,'')) like :q)"
            params["q"] = "%" + q.lower() + "%"
        if department_id: where += " and e.department_id=:dept"; params["dept"] = department_id
        if location_id: where += " and e.location_id=:loc"; params["loc"] = location_id
        if status: where += " and e.status=:status"; params["status"] = status
        total = one("select count(*) n from employee e" + where, **params)["n"]
        rows = many("select e.id,e.emp_code,e.name,e.email,e.status,e.join_date,e.birth_date,e.manager_id,d.name department,g.title designation,g.level,l.id location_id,l.name location,l.type location_type from employee e join department d on d.id=e.department_id join designation g on g.id=e.designation_id join location l on l.id=e.location_id" + where + " order by e.name limit :lim offset :off", **params, lim=size, off=(page-1)*size)
        return dict(items=rows, total=total, page=page, size=size)

    @app.get("/api/people/employees/{eid}")
    def employee_detail(eid: int, u=Depends(read)):
        e = employee(eid, u)
        e["manager"] = one("select id,name,emp_code from employee where id=:i", i=e["manager_id"]) if e["manager_id"] else None
        e["attendance"] = many("select date,punch_in,punch_out,hours,status,source from attendance where employee_id=:i order by date desc limit 14", i=eid)
        e["leaves"] = many("select r.id,t.code leave_code,t.name leave_type,r.from_date,r.to_date,r.status,r.is_wfh from leave_request r join leave_type t on t.id=r.leave_type_id where r.employee_id=:i order by r.from_date desc limit 12", i=eid)
        e["onboarding"] = many("select id,task,due_date,done_at from onboarding_task where employee_id=:i order by due_date", i=eid)
        return e

    @app.get("/api/people/attendance")
    def attendance(from_date: date = None, to_date: date = None, location_id: int = 0, status: str = "", q: str = "", page: int = 1, size: int = 50, u=Depends(read)):
        page, size = paged(page, size)
        start = from_date or today - timedelta(days=13); end = to_date or today
        if end < start or (end-start).days > 92: raise HTTPException(422, "Choose a date range of up to 93 days")
        extra, params = scoped(u)
        params.update(start=start, end=end)
        where = " where a.date between :start and :end" + extra
        if location_id: where += " and e.location_id=:loc"; params["loc"] = location_id
        if status: where += " and a.status=:status"; params["status"] = status
        if q: where += " and (lower(e.name) like :q or lower(e.emp_code) like :q)"; params["q"] = "%" + q.lower() + "%"
        total = one("select count(*) n from attendance a join employee e on e.id=a.employee_id" + where, **params)["n"]
        items = many("select a.id,a.date,a.punch_in,a.punch_out,a.hours,a.status,a.source,e.id employee_id,e.emp_code,e.name,d.name department,l.name location from attendance a join employee e on e.id=a.employee_id join department d on d.id=e.department_id join location l on l.id=e.location_id" + where + " order by a.date desc,e.name limit :lim offset :off", **params, lim=size, off=(page-1)*size)
        return dict(items=items,total=total,page=page,size=size,from_date=str(start),to_date=str(end))

    @app.post("/api/people/attendance")
    def mark_attendance(b: AttendanceIn, u=Depends(manager)):
        if u["role"] not in HR and u["role"] not in ("store_manager", "warehouse"):
            raise HTTPException(403, "Your role cannot record attendance")
        if b.status not in attendance_statuses: raise HTTPException(422, "Unknown attendance status")
        e = employee(b.employee_id, u)
        if b.date > today: raise HTTPException(422, "Attendance cannot be recorded for a future date")
        with engine.begin() as c:
            old = c.execute(text("select id,status,punch_in,punch_out from attendance where employee_id=:e and date=:d order by id desc limit 1"), {"e":b.employee_id,"d":b.date}).mappings().first()
            values = dict(employee_id=b.employee_id,date=b.date,status=b.status,punch_in=b.punch_in,punch_out=b.punch_out,source=b.source or "manual",location_id=e["location_id"],hours=round((b.punch_out-b.punch_in).total_seconds()/3600,2) if b.punch_in and b.punch_out else None)
            if old:
                c.execute(text("update attendance set status=:status,punch_in=:punch_in,punch_out=:punch_out,source=:source,hours=:hours where id=:id"),dict(values,id=old["id"]))
                rid=old["id"]; action="update"
            else:
                rid=c.execute(text("insert into attendance(employee_id,date,punch_in,punch_out,source,location_id,hours,status) values (:employee_id,:date,:punch_in,:punch_out,:source,:location_id,:hours,:status) returning id"),values).scalar()
                action="create"
            audit(c,u,action,"attendance",rid,serial(dict(old)) if old else None,serial(values))
        return dict(id=rid,ok=True)

    @app.get("/api/people/roster")
    def roster(from_date: date = None, to_date: date = None, location_id: int = 0, u=Depends(read)):
        start=from_date or today; end=to_date or today+timedelta(days=6)
        if end < start or (end-start).days > 31: raise HTTPException(422,"Choose a roster window of up to 32 days")
        extra,params=scoped(u); params.update(start=start,end=end)
        where=" where r.date between :start and :end"+extra
        if location_id: where+=" and e.location_id=:loc";params["loc"]=location_id
        return many("select r.id,r.date,e.id employee_id,e.emp_code,e.name,d.name department,l.id location_id,l.name location,s.code shift_code,s.start_time,s.end_time from roster r join employee e on e.id=r.employee_id join department d on d.id=e.department_id join location l on l.id=r.location_id join shift s on s.id=r.shift_id"+where+" order by r.date,l.name,e.name limit 600",**params)

    @app.post("/api/people/roster")
    def assign_roster(b: RosterIn, u=Depends(hr)):
        e=employee(b.employee_id,u)
        if e["location_id"]!=b.location_id: raise HTTPException(422,"Roster location must match the employee's assigned location")
        if not one("select id from shift where id=:i",i=b.shift_id): raise HTTPException(422,"Unknown shift")
        with engine.begin() as c:
            old=c.execute(text("select id from roster where employee_id=:e and date=:d order by id desc limit 1"),{"e":b.employee_id,"d":b.date}).scalar()
            if old:
                c.execute(text("update roster set shift_id=:s,location_id=:l where id=:i"),{"s":b.shift_id,"l":b.location_id,"i":old}); rid=old
            else:
                rid=c.execute(text("insert into roster(employee_id,date,shift_id,location_id) values (:e,:d,:s,:l) returning id"),{"e":b.employee_id,"d":b.date,"s":b.shift_id,"l":b.location_id}).scalar()
            audit(c,u,"assign","roster",rid,None,serial(b.dict()))
        return dict(id=rid,ok=True)

    @app.get("/api/people/leaves")
    def leaves(status: str = "", employee_id: int = 0, u=Depends(read)):
        extra,params=scoped(u); where=" where 1=1"+extra
        if status: where+=" and r.status=:st";params["st"]=status
        if employee_id: where+=" and e.id=:eid";params["eid"]=employee_id
        return many("select r.id,r.from_date,r.to_date,r.status,r.is_wfh,r.approver_id,e.id employee_id,e.emp_code,e.name,d.name department,l.name location,t.code leave_code,t.name leave_type,t.annual_quota from leave_request r join employee e on e.id=r.employee_id join department d on d.id=e.department_id join location l on l.id=e.location_id join leave_type t on t.id=r.leave_type_id"+where+" order by case when r.status='pending' then 0 else 1 end,r.from_date desc limit 300",**params)

    @app.post("/api/people/leaves")
    def create_leave(b: LeaveIn, u=Depends(hr)):
        e=employee(b.employee_id,u)
        if b.to_date < b.from_date: raise HTTPException(422,"End date must be on or after start date")
        if (b.to_date-b.from_date).days > 60: raise HTTPException(422,"A single leave request cannot exceed 61 calendar days")
        if not one("select id from leave_type where id=:i",i=b.leave_type_id): raise HTTPException(422,"Unknown leave type")
        with engine.begin() as c:
            rid=c.execute(text("insert into leave_request(employee_id,leave_type_id,from_date,to_date,status,is_wfh,approver_id) values (:e,:t,:f,:to,'pending',:w,null) returning id"),{"e":b.employee_id,"t":b.leave_type_id,"f":b.from_date,"to":b.to_date,"w":b.is_wfh}).scalar()
            audit(c,u,"create","leave_request",rid,None,serial(b.dict()))
        return dict(id=rid,status="pending")

    @app.post("/api/people/leaves/{rid}/decision")
    def decide_leave(rid: int,b: DecisionIn,u=Depends(manager)):
        if b.decision not in ("approve","reject"): raise HTTPException(422,"Decision must be approve or reject")
        r=one("select r.*,e.location_id from leave_request r join employee e on e.id=r.employee_id where r.id=:i",i=rid)
        if not r: raise HTTPException(404,"Leave request not found")
        extra,params=scoped(u)
        if extra and not many("select e.id from employee e where e.id=:i"+extra,i=r["employee_id"],**params): raise HTTPException(404,"Leave request not found")
        if r["status"]!="pending": raise HTTPException(409,"This request has already been decided")
        approver = one("select employee_id from app_user where id=:i", i=u["id"])
        with engine.begin() as c:
            c.execute(text("update leave_request set status=:s,approver_id=:u where id=:i"),{"s":"approved" if b.decision=="approve" else "rejected","u":approver["employee_id"] if approver else None,"i":rid})
            audit(c,u,b.decision,"leave_request",rid,serial(dict(r)),{"status":"approved" if b.decision=="approve" else "rejected","note":b.note})
        return dict(id=rid,status="approved" if b.decision=="approve" else "rejected")

    @app.get("/api/people/onboarding")
    def onboarding(open_only: bool = True, q: str = "", u=Depends(read)):
        extra,params=scoped(u); where=" where 1=1"+extra
        if open_only: where+=" and t.done_at is null"
        if q: where+=" and (lower(e.name) like :q or lower(t.task) like :q)";params["q"]="%"+q.lower()+"%"
        return many("select t.id,t.task,t.due_date,t.done_at,e.id employee_id,e.emp_code,e.name,d.name department,l.name location from onboarding_task t join employee e on e.id=t.employee_id join department d on d.id=e.department_id join location l on l.id=e.location_id"+where+" order by t.due_date,e.name limit 400",**params)

    @app.patch("/api/people/onboarding/{tid}")
    def complete_onboarding(tid: int,u=Depends(hr)):
        r=one("select id,employee_id,task,done_at from onboarding_task where id=:i",i=tid)
        if not r: raise HTTPException(404,"Onboarding task not found")
        done=None if r["done_at"] else NOW
        with engine.begin() as c:
            c.execute(text("update onboarding_task set done_at=:d where id=:i"),{"d":done,"i":tid})
            audit(c,u,"toggle","onboarding_task",tid,serial(dict(r)),{"done_at":done.isoformat() if done else None})
        return dict(id=tid,done_at=done)

    @app.get("/api/people/jobs")
    def jobs(u=Depends(hr)):
        return many("select j.id,j.title,j.status,j.posted_on,j.closes_on,j.description,d.name department,l.name location,(select count(*) from applicant a where a.job_id=j.id) applicants from job_posting j join department d on d.id=j.department_id left join location l on l.id=j.location_id order by case when j.status='open' then 0 else 1 end,j.posted_on desc")

    @app.post("/api/people/jobs")
    def create_job(b: JobIn,u=Depends(hr)):
        if not b.title.strip(): raise HTTPException(422,"Job title is required")
        if b.closes_on and b.closes_on < b.posted_on: raise HTTPException(422,"Closing date cannot precede posting date")
        if not one("select id from department where id=:i",i=b.department_id): raise HTTPException(422,"Unknown department")
        with engine.begin() as c:
            jid=c.execute(text("insert into job_posting(title,department_id,location_id,status,posted_on,closes_on,description) values (:t,:d,:l,'open',:p,:c,:x) returning id"),{"t":b.title.strip(),"d":b.department_id,"l":b.location_id,"p":b.posted_on,"c":b.closes_on,"x":b.description}).scalar()
            audit(c,u,"create","job_posting",jid,None,serial(b.dict()))
        return dict(id=jid,status="open")

    @app.get("/api/people/applicants")
    def applicants(job_id: int = 0, stage: str = "", u=Depends(hr)):
        w=" where 1=1";p={}
        if job_id:w+=" and a.job_id=:j";p["j"]=job_id
        if stage:w+=" and a.stage=:s";p["s"]=stage
        return many("select a.id,a.job_id,a.name,a.stage,a.applied_on,a.source,j.title job_title,d.name department from applicant a join job_posting j on j.id=a.job_id join department d on d.id=j.department_id"+w+" order by a.applied_on desc,a.name limit 500",**p)

    @app.patch("/api/people/applicants/{aid}")
    def applicant_stage(aid: int,b: ApplicantStageIn,u=Depends(hr)):
        if b.stage not in stages: raise HTTPException(422,"Unknown applicant stage")
        old=one("select id,name,stage,job_id from applicant where id=:i",i=aid)
        if not old: raise HTTPException(404,"Applicant not found")
        with engine.begin() as c:
            c.execute(text("update applicant set stage=:s where id=:i"),{"s":b.stage,"i":aid})
            audit(c,u,"stage_change","applicant",aid,dict(old),{"stage":b.stage})
        return dict(id=aid,stage=b.stage)

    @app.get("/api/people/announcements")
    def announcements(u=Depends(read)):
        return many("select id,title,body,created_at,audience from announcement order by created_at desc limit 30")
