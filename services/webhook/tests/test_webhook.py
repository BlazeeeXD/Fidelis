"""
Webhook service tests. The Orchestrator is faked, so no network is needed.

Run from services/webhook/:
    pytest tests/
"""
import hashlib
import hmac
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ["GITHUB_WEBHOOK_SECRET"] = "testsecret"

from app import main as webhook_main
from app.main import app
from app.orchestrator_client import (
    OrchestratorError,
    OrchestratorRejectedError,
)

client = TestClient(app)
SAMPLE = (Path(__file__).parent / "sample_payloads" / "pull_request_opened.json").read_bytes()


def _sign(body: bytes, secret: str = "testsecret") -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _headers(delivery: str, body: bytes = SAMPLE, event: str = "pull_request") -> dict:
    return {
        "X-GitHub-Event": event,
        "X-Hub-Signature-256": _sign(body),
        "X-GitHub-Delivery": delivery,
    }


@pytest.fixture(autouse=True)
def fake_orchestrator(monkeypatch):
    """Replace the real orchestrator call; records what the webhook sent."""
    calls = []

    async def fake_create_job(repository, pr_number, commit_sha):
        calls.append(
            {"repository": repository, "pr_number": pr_number, "commit_sha": commit_sha}
        )
        return {"job_id": "job-123", "status": "QUEUED", "duplicate": False}

    monkeypatch.setattr(webhook_main.orchestrator, "create_job", fake_create_job)
    return calls


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


def test_accepts_valid_pr_opened_event(fake_orchestrator):
    resp = client.post(
        "/webhook/github", content=SAMPLE, headers=_headers("test-delivery-1")
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "queued"
    assert body["pr"] == 42
    assert body["job_id"] == "job-123"

    # The webhook sent the orchestrator exactly the contract fields.
    assert len(fake_orchestrator) == 1
    sent = fake_orchestrator[0]
    assert sent["pr_number"] == 42
    assert "/" in sent["repository"]
    assert sent["commit_sha"]


def test_ignores_non_trigger_action(fake_orchestrator):
    payload = json.loads(SAMPLE)
    payload["action"] = "closed"
    body = json.dumps(payload).encode()
    resp = client.post(
        "/webhook/github", content=body, headers=_headers("test-delivery-2", body)
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
    assert fake_orchestrator == []  # no job created


def test_ignores_non_pr_event(fake_orchestrator):
    resp = client.post(
        "/webhook/github",
        content=SAMPLE,
        headers=_headers("test-delivery-3", event="push"),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
    assert fake_orchestrator == []


def test_same_delivery_id_is_only_processed_once(fake_orchestrator):
    h = _headers("test-delivery-dupe")
    first = client.post("/webhook/github", content=SAMPLE, headers=h)
    second = client.post("/webhook/github", content=SAMPLE, headers=h)

    assert first.json()["status"] == "queued"
    assert second.json()["status"] == "duplicate_ignored"
    assert len(fake_orchestrator) == 1


def test_orchestrator_down_returns_502_and_allows_redelivery(monkeypatch):
    state = {"down": True}

    async def flaky(repository, pr_number, commit_sha):
        if state["down"]:
            raise OrchestratorError("orchestrator is down")
        return {"job_id": "job-456", "status": "QUEUED", "duplicate": False}

    monkeypatch.setattr(webhook_main.orchestrator, "create_job", flaky)
    h = _headers("test-delivery-retry")

    first = client.post("/webhook/github", content=SAMPLE, headers=h)
    assert first.status_code == 502

    # GitHub redelivers the SAME delivery ID after the orchestrator recovers.
    state["down"] = False
    second = client.post("/webhook/github", content=SAMPLE, headers=h)
    assert second.status_code == 200
    assert second.json()["status"] == "queued"
    assert second.json()["job_id"] == "job-456"


def test_orchestrator_rejection_returns_500(monkeypatch):
    async def rejecting(repository, pr_number, commit_sha):
        raise OrchestratorRejectedError("422 bad payload")

    monkeypatch.setattr(webhook_main.orchestrator, "create_job", rejecting)
    resp = client.post(
        "/webhook/github", content=SAMPLE, headers=_headers("test-delivery-reject")
    )
    assert resp.status_code == 500