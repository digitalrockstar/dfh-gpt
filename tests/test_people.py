import os, shutil, pytest
from datetime import date, timedelta
from fastapi.testclient import TestClient
from sqlalchemy import text
from dfh.app import create_app
from dfh.db import get_engine

SRC = os.getenv("DFH_DB", "sqlite:///data/dfh.db").replace("sqlite:///", "")


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    p = str(tmp_path_factory.mktemp("people") / "dfh.db")
    shutil.copy(SRC, p)
    url = "sqlite:///" + p
    return TestClient(create_app(url, os.getenv("DFH_DATA", "data"), serve_frontend=False)), get_engine(url)


def token(c, user="ceo"):
    r = c.post("/api/login", json={"username": user, "password": "demo1234"})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def q(engine, sql, **params):
    with engine.connect() as c:
        return c.execute(text(sql), params).fetchall()


def test_people_seed_and_permissions(env):
    c, e = env
    h = token(c, "hr")
    opts = c.get("/api/people/options", headers=h).json()
    assert opts["can_manage"] and len(opts["departments"]) >= 8 and len(opts["shifts"]) >= 3
    s = c.get("/api/people/summary", headers=h).json()
    assert s["active"] >= 2300 and s["attendance"]["records"] > 0
    assert s["pending_leaves"] > 0 and s["open_jobs"] >= 5
    assert c.get("/api/people/summary", headers=token(c, "cashier")).status_code == 403
    assert c.get("/api/people/employees", headers=token(c, "finance")).status_code == 403
    rows = c.get("/api/people/employees?size=10", headers=h).json()
    assert rows["total"] >= 2300 and len(rows["items"]) == 10
    assert q(e, "select count(*) from app_user where employee_id is not null")[0][0] >= 6
    sm = c.get("/api/people/employees?size=100", headers=token(c, "storemgr")).json()
    assert sm["total"] > 0 and all(x["location_type"] == "store" for x in sm["items"])
    wh = c.get("/api/people/employees?size=100", headers=token(c, "warehouse")).json()
    assert wh["total"] > 0 and all(x["location_type"] == "warehouse" for x in wh["items"])


def test_employee_profile_and_attendance(env):
    c, e = env; h = token(c, "hr")
    eid = q(e, "select id from employee where status='active' and join_date<='2026-10-31' order by id limit 1")[0][0]
    detail = c.get(f"/api/people/employees/{eid}", headers=h)
    assert detail.status_code == 200 and detail.json()["department"] and detail.json()["designation"]
    assert detail.json()["attendance"]
    assert c.get("/api/people/attendance?from_date=2026-10-18&to_date=2026-10-31", headers=h).json()["total"] > 0
    assert c.get("/api/people/attendance?from_date=2026-01-01&to_date=2026-10-31", headers=h).status_code == 422
    body = {"employee_id": eid, "date": "2026-10-31", "status": "late", "source": "manual"}
    r = c.post("/api/people/attendance", headers=h, json=body)
    assert r.status_code == 200, r.text
    assert c.post("/api/people/attendance", headers=h, json={**body, "date": "2026-11-01"}).status_code == 422
    assert c.post("/api/people/attendance", headers=token(c, "storemgr"), json=body).status_code in (200, 404)
    assert q(e, "select count(*) from audit_log where table_name='attendance' and row_id=:i", i=str(r.json()["id"]))[0][0] == 1


def test_roster_and_leave_workflow(env):
    c, e = env; h = token(c, "hr")
    eid, loc = q(e, "select id,location_id from employee where status='active' order by id limit 1")[0]
    shift = q(e, "select id from shift order by id limit 1")[0][0]
    body = {"employee_id": eid, "date": "2026-11-02", "shift_id": shift, "location_id": loc}
    r = c.post("/api/people/roster", headers=h, json=body)
    assert r.status_code == 200, r.text
    assert c.get("/api/people/roster?from_date=2026-11-02&to_date=2026-11-02", headers=h).json()
    assert c.post("/api/people/roster", headers=token(c, "storemgr"), json=body).status_code == 403
    leave_type = q(e, "select id from leave_type order by id limit 1")[0][0]
    leave = c.post("/api/people/leaves", headers=h, json={"employee_id":eid,"leave_type_id":leave_type,"from_date":"2026-11-10","to_date":"2026-11-11"})
    assert leave.status_code == 200, leave.text
    rid = leave.json()["id"]
    assert c.post(f"/api/people/leaves/{rid}/decision", headers=h, json={"decision":"approve"}).json()["status"] == "approved"
    assert c.post(f"/api/people/leaves/{rid}/decision", headers=h, json={"decision":"reject"}).status_code == 409
    assert c.post("/api/people/leaves", headers=token(c, "storemgr"), json={"employee_id":eid,"leave_type_id":leave_type,"from_date":"2026-11-10","to_date":"2026-11-11"}).status_code == 403
    assert c.post("/api/people/leaves", headers=h, json={"employee_id":eid,"leave_type_id":leave_type,"from_date":"2026-11-12","to_date":"2026-11-10"}).status_code == 422


def test_onboarding_and_hiring(env):
    c, e = env; h = token(c, "hr")
    tasks = c.get("/api/people/onboarding", headers=h).json()
    if tasks:
        tid = tasks[0]["id"]
        assert c.patch(f"/api/people/onboarding/{tid}", headers=h).status_code == 200
        assert c.patch(f"/api/people/onboarding/{tid}", headers=h).json()["done_at"] is None
    assert c.patch("/api/people/onboarding/999999", headers=h).status_code == 404
    opts = c.get("/api/people/options", headers=h).json()
    job = c.post("/api/people/jobs", headers=h, json={"title":"People Partner","department_id":next(x["id"] for x in opts["departments"] if x["name"]=="HR"),"posted_on":"2026-10-31","closes_on":"2026-11-30","description":"Build a great employee experience."})
    assert job.status_code == 200, job.text
    apps = c.get("/api/people/applicants", headers=h).json()
    assert len(apps) >= 100
    aid = apps[0]["id"]
    assert c.patch(f"/api/people/applicants/{aid}", headers=h, json={"stage":"interview"}).json()["stage"] == "interview"
    assert c.patch(f"/api/people/applicants/{aid}", headers=h, json={"stage":"unknown"}).status_code == 422
    assert c.get("/api/people/jobs", headers=h).json()
    assert c.get("/api/people/announcements", headers=h).json()
