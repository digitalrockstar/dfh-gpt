import os

import pytest
from fastapi.testclient import TestClient

from dfh.app import create_app


DATABASE_URL = os.getenv("DFH_TEST_POSTGRES_SEED_URL")
DATA_DIR = os.getenv("DFH_TEST_POSTGRES_DATA", "data_pg")


@pytest.mark.skipif(not DATABASE_URL, reason="PostgreSQL seeded runtime URL is not configured")
def test_seeded_application_routes_on_postgresql():
    app = create_app(DATABASE_URL, DATA_DIR, serve_frontend=False)
    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["ok"] is True

        login = client.post(
            "/api/login",
            json={"username": "ceo", "password": "demo1234"},
        )
        assert login.status_code == 200, login.text
        headers = {"Authorization": "Bearer " + login.json()["token"]}

        checks = [
            ("/api/skus?size=5", "items"),
            ("/api/ct/summary?days=30", "current"),
            ("/api/inv/expiry?days=60", "items"),
            ("/api/procurement/shipments", "items"),
            ("/api/wastage/expiry?window=60", "rows"),
            ("/api/finance/summary", "cash"),
        ]
        for path, expected_key in checks:
            response = client.get(path, headers=headers)
            assert response.status_code == 200, f"{path}: {response.text}"
            assert expected_key in response.json(), f"{path}: missing {expected_key}"
