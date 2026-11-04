import os, shutil
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from dfh.app import create_app
from dfh.db import get_engine

SRC = os.getenv("DFH_DB", "sqlite:///data/dfh.db").replace("sqlite:///", "")


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    p = str(tmp_path_factory.mktemp("platform") / "dfh.db")
    shutil.copy(SRC, p)
    url = "sqlite:///" + p
    return TestClient(create_app(url, os.getenv("DFH_DATA", "data"), serve_frontend=False)), get_engine(url)


def token(c, user="ceo"):
    r = c.post("/api/login", json={"username": user, "password": "demo1234"})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def test_platform_read_scopes_and_summary(env):
    c, _ = env
    admin = token(c)
    assert c.get("/api/platform/options", headers=admin).json()["can_admin"]
    summary = c.get("/api/platform/summary", headers=admin)
    assert summary.status_code == 200 and summary.json()["users"]["active"] >= 9
    assert c.get("/api/platform/users", headers=token(c, "finance")).status_code == 200
    assert c.get("/api/platform/users", headers=token(c, "cashier")).status_code == 403
    assert c.get("/api/platform/audit", headers=token(c, "finance")).status_code == 200
    assert c.get("/api/platform/summary", headers=token(c, "cashier")).status_code == 403


def test_user_access_update_is_admin_only_and_scoped(env):
    c, e = env
    admin = token(c)
    users = c.get("/api/platform/users", headers=admin).json()["items"]
    cashier = next(x for x in users if x["username"] == "cashier")
    assert c.patch(f"/api/platform/users/{cashier['id']}", headers=token(c, "finance"), json={"active": True}).status_code == 403
    assert c.patch(f"/api/platform/users/{cashier['id']}", headers=admin, json={"active": True}).status_code == 200
    assert c.patch(f"/api/platform/users/{cashier['id']}", headers=admin, json={"scope_type": "store", "scope_id": 999999}).status_code == 422
    assert c.patch(f"/api/platform/users/{cashier['id']}", headers=admin, json={"active": False}).status_code == 200
    assert c.patch(f"/api/platform/users/{cashier['id']}", headers=admin, json={"active": True}).status_code == 200
    me = next(x for x in users if x["username"] == "ceo")
    assert c.patch(f"/api/platform/users/{me['id']}", headers=admin, json={"active": False}).status_code == 409
    assert e is not None


def test_approval_rules_request_and_decision(env):
    c, e = env
    admin = token(c)
    roles = c.get("/api/platform/options", headers=admin).json()["roles"]
    finance_role = next(x["id"] for x in roles if x["code"] == "finance")
    rule = c.post("/api/platform/approval-rules", headers=admin, json={
        "doc_type": "manual", "min_amount_paise": 100000, "max_amount_paise": 500000,
        "approver_role_id": finance_role
    })
    assert rule.status_code == 200, rule.text
    assert c.post("/api/platform/approval-rules", headers=admin, json={
        "doc_type": "manual", "min_amount_paise": 200000, "max_amount_paise": 600000,
        "approver_role_id": finance_role
    }).status_code == 409
    requester = token(c, "procurement")
    request = c.post("/api/platform/approvals", headers=requester, json={
        "doc_type": "manual", "doc_id": "TEST-APP-1", "amount_paise": 250000, "note": "Test approval"
    })
    assert request.status_code == 200, request.text
    rid = request.json()["id"]
    assert c.post(f"/api/platform/approvals/{rid}/decision", headers=requester, json={"decision": "approve"}).status_code == 403
    finance = token(c, "finance")
    assert c.post(f"/api/platform/approvals/{rid}/decision", headers=finance, json={"decision": "approve"}).json()["status"] == "approved"
    assert c.post(f"/api/platform/approvals/{rid}/decision", headers=finance, json={"decision": "reject"}).status_code == 409
    rows = c.get("/api/platform/approvals?status=approved", headers=admin).json()["items"]
    assert any(x["id"] == rid for x in rows)
    assert c.get("/api/platform/audit?action=create&table_name=approval_rule", headers=admin).json()["total"] >= 1
    with e.connect() as db:
        assert db.execute(text("select status from approval_request where id=:i"), {"i": rid}).scalar() == "approved"


def test_sync_imports_entities_and_fx(env):
    c, e = env
    admin = token(c)
    assert c.get("/api/platform/sync", headers=admin).status_code == 200
    assert c.get("/api/platform/imports", headers=admin).status_code == 200
    entities = c.get("/api/platform/entities", headers=admin).json()
    assert len(entities["items"]) >= 5
    body = {"currency": "XTS", "month": "2026-11-01", "rate_to_inr": 1.25, "assumed": True}
    created = c.post("/api/platform/fx", headers=admin, json=body)
    assert created.status_code == 200, created.text
    updated = c.post("/api/platform/fx", headers=admin, json={**body, "rate_to_inr": 1.5, "assumed": False})
    assert updated.status_code == 200 and updated.json()["updated"]
    assert c.get("/api/platform/entities", headers=admin).json()["fx"][0]["currency"] in ("XTS", "USD", "AED", "INR")
    assert c.post("/api/platform/fx", headers=token(c, "finance"), json={**body, "month": "2026-11-02"}).status_code == 403
    assert c.post("/api/platform/fx", headers=admin, json={**body, "month": "2026-11-02"}).status_code == 422
    with e.connect() as db:
        assert db.execute(text("select rate_to_inr from fx_rate where currency='XTS' and month='2026-11-01'")).scalar() == 1.5
