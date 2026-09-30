"""
Minimal gateway tests for CI.

Run from inside services/gateway/:
    pytest tests/test_gateway.py
"""
from app.main import app
from fastapi.testclient import TestClient

client = TestClient(app)


def test_healthz_returns_200():
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_orchestrator_route_requires_bearer_token():
    response = client.get("/api/orchestrator/jobs")
    assert response.status_code == 401


def test_dev_token_returns_access_token():
    response = client.post("/auth/dev-token")
    assert response.status_code == 200
    body = response.json()
    assert "access_token" in body
    assert isinstance(body["access_token"], str)
    assert body["access_token"]
