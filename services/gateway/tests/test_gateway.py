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


def test_dashboard_is_served_from_gateway():
    response = client.get("/")
    assert response.status_code == 200
    assert "FIDELIS" in response.text
    assert "Asynchronous, multi-agent pull-request review" in response.text
    assert "Redis/Celery" in response.text
    assert "sequential local, single-pass or parallel execution" in response.text


def test_demo_replay_requires_a_previous_event():
    from app import main

    main._last_demo_delivery = None
    response = client.post("/demo-api/replay-last")
    assert response.status_code == 409
    assert "Send a sample PR event" in response.json()["detail"]


def test_demo_event_rejects_unsupported_action():
    response = client.post("/demo-api/events?action=closed")
    assert response.status_code == 422
    assert "Action must be" in response.json()["detail"]


def test_demo_event_endpoint_rejects_get_requests():
    """Creating a demo event is state-changing and must remain POST-only."""
    response = client.get("/demo-api/events?action=opened")
    assert response.status_code == 405


def test_demo_event_endpoint_is_disabled_in_production(monkeypatch):
    from app import main

    monkeypatch.setattr(main, "ENVIRONMENT", "production")
    response = client.post("/demo-api/events?action=opened")
    assert response.status_code == 404
    assert "disabled" in response.json()["detail"]


def test_dev_token_endpoint_is_disabled_in_production(monkeypatch):
    from app import main

    monkeypatch.setattr(main, "ENVIRONMENT", "production")
    response = client.post("/auth/dev-token")
    assert response.status_code == 404
    assert "disabled" in response.json()["detail"]


def test_demo_event_is_hmac_signed_and_replay_reuses_same_delivery(monkeypatch):
    import hashlib
    import hmac
    import json

    import httpx
    from app import main

    monkeypatch.setattr(main, "ENVIRONMENT", "development")
    monkeypatch.setattr(main, "GITHUB_WEBHOOK_SECRET", "test-demo-secret")
    monkeypatch.setattr(main, "_last_demo_delivery", None)
    calls = []

    async def fake_post(url, *, content, headers, **kwargs):
        calls.append({"url": url, "content": content, "headers": dict(headers)})
        if len(calls) == 1:
            return httpx.Response(200, json={"status": "queued", "job_id": "demo-job"})
        return httpx.Response(200, json={"status": "duplicate_ignored"})

    monkeypatch.setattr(main._http_client, "post", fake_post)
    try:
        created = client.post("/demo-api/events?action=opened")
        assert created.status_code == 200
        first = created.json()
        assert first["result"]["status"] == "queued"
        assert len(first["commit_sha"]) == 40

        expected_signature = "sha256=" + hmac.new(
            b"test-demo-secret", calls[0]["content"], hashlib.sha256
        ).hexdigest()
        assert calls[0]["headers"]["X-Hub-Signature-256"] == expected_signature
        assert calls[0]["headers"]["X-GitHub-Delivery"] == first["delivery_id"]
        payload = json.loads(calls[0]["content"])
        assert payload["pull_request"]["head"]["sha"] == first["commit_sha"]

        replay = client.post("/demo-api/replay-last")
        assert replay.status_code == 200
        assert replay.json()["result"]["status"] == "duplicate_ignored"
        assert calls[1]["content"] == calls[0]["content"]
        assert calls[1]["headers"]["X-GitHub-Delivery"] == calls[0]["headers"]["X-GitHub-Delivery"]
    finally:
        main._last_demo_delivery = None


def test_sample_event_button_uses_post_method():
    """The dashboard must use POST for the state-changing demo endpoint."""
    response = client.get("/")
    assert response.status_code == 200
    assert "getJson('/demo-api/events?action=opened',{method:'POST'})" in response.text
