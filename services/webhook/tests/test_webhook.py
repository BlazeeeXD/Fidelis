"""
Minimal tests to get CI green early. Expand alongside real
Orchestrator integration in Weeks 5-6.

Run with:
    pip install pytest fastapi[all]
    pytest webhook/test_webhook.py
"""
import hashlib
import hmac
import json
import os
from pathlib import Path

from fastapi.testclient import TestClient

os.environ["GITHUB_WEBHOOK_SECRET"] = "testsecret"

from app.main import app

client = TestClient(app)
SAMPLE = (Path(__file__).parent / "sample_payloads" / "pull_request_opened.json").read_bytes()
# Run from services/webhook/: pytest tests/test_webhook.py


def _sign(body: bytes, secret: str = "testsecret") -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_healthz():
    resp = client.get("/healthz")
    assert resp.status_code == 200


def test_rejects_missing_signature():
    resp = client.post(
        "/webhook/github",
        content=SAMPLE,
        headers={"X-GitHub-Event": "pull_request"},
    )
    assert resp.status_code == 401


def test_rejects_bad_signature():
    resp = client.post(
        "/webhook/github",
        content=SAMPLE,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": "sha256=deadbeef",
        },
    )
    assert resp.status_code == 401


def test_accepts_valid_pr_opened_event():
    resp = client.post(
        "/webhook/github",
        content=SAMPLE,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": _sign(SAMPLE),
            "X-GitHub-Delivery": "test-delivery-1",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "queued"
    assert body["pr"] == 42


def test_ignores_non_trigger_action():
    payload = json.loads(SAMPLE)
    payload["action"] = "closed"
    body = json.dumps(payload).encode()
    resp = client.post(
        "/webhook/github",
        content=body,
        headers={
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": _sign(body),
            "X-GitHub-Delivery": "test-delivery-2",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


def test_ignores_non_pr_event():
    resp = client.post(
        "/webhook/github",
        content=SAMPLE,
        headers={
            "X-GitHub-Event": "push",
            "X-Hub-Signature-256": _sign(SAMPLE),
            "X-GitHub-Delivery": "test-delivery-3",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
