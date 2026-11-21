"""
Week 2 webhook hardening tests (P2).

Covers: content-type handling (JSON + form-encoded), lenient action
parsing, bounded LRU idempotency, and error mapping. The Orchestrator
is faked, same as test_webhook.py.

Run from services/webhook/:
    pytest tests/
"""
import hashlib
import hmac
import json
import os
from pathlib import Path
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

os.environ["GITHUB_WEBHOOK_SECRET"] = "testsecret"

from app import main as webhook_main
from app.main import app
from app.orchestrator_client import OrchestratorError

PAYLOADS = Path(__file__).parent / "sample_payloads"
SAMPLE = (PAYLOADS / "pull_request_opened.json").read_bytes()
SYNC = (PAYLOADS / "pull_request_synchronize.json").read_bytes()
CLOSED = (PAYLOADS / "pull_request_closed.json").read_bytes()
REOPENED = (PAYLOADS / "pull_request_reopened.json").read_bytes()
PING = (PAYLOADS / "ping.json").read_bytes()

client = TestClient(app)

REPO = "octo-org/pr-reviewer-demo"
OPENED_HEAD_SHA = "abc123def456"
SYNC_HEAD_SHA = "c0ffee123456"


def _sign(body: bytes, secret: str = "testsecret") -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _headers(
    delivery: str,
    body: bytes,
    event: str = "pull_request",
    content_type: str | None = None,
) -> dict:
    headers = {
        "X-GitHub-Event": event,
        "X-Hub-Signature-256": _sign(body),
        "X-GitHub-Delivery": delivery,
    }
    if content_type is not None:
        headers["Content-Type"] = content_type
    return headers


@pytest.fixture(autouse=True)
def clear_dedupe():
    """Isolate tests from delivery IDs left behind by other tests."""
    webhook_main._seen_delivery_ids.clear()
    yield
    webhook_main._seen_delivery_ids.clear()


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


def test_synchronize_forwards_job_fields(fake_orchestrator):
    resp = client.post(
        "/webhook/github", content=SYNC, headers=_headers("hard-sync-1", SYNC)
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"
    assert fake_orchestrator == [
        {"repository": REPO, "pr_number": 42, "commit_sha": SYNC_HEAD_SHA}
    ]


def test_reopened_forwards_job_fields(fake_orchestrator):
    resp = client.post(
        "/webhook/github", content=REOPENED, headers=_headers("hard-reopen-1", REOPENED)
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"
    assert fake_orchestrator == [
        {"repository": REPO, "pr_number": 42, "commit_sha": OPENED_HEAD_SHA}
    ]


def test_closed_action_is_ignored(fake_orchestrator):
    resp = client.post(
        "/webhook/github", content=CLOSED, headers=_headers("hard-closed-1", CLOSED)
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored", "action": "closed"}
    assert fake_orchestrator == []


def test_ping_returns_pong(fake_orchestrator):
    resp = client.post(
        "/webhook/github", content=PING, headers=_headers("hard-ping-1", PING, event="ping")
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "pong"}
    assert fake_orchestrator == []


def test_minimal_non_trigger_payload_is_ignored_not_422(fake_orchestrator):
    """An irrelevant action with an otherwise missing payload is NOT a 422."""
    body = b'{"action": "labeled"}'
    resp = client.post(
        "/webhook/github", content=body, headers=_headers("hard-labeled-1", body)
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored", "action": "labeled"}
    assert fake_orchestrator == []


def test_form_encoded_delivery_is_queued(fake_orchestrator):
    """GitHub's form content type: body is payload=<urlencoded json>.

    The signature must cover the RAW form body, not the inner JSON.
    """
    form_body = urlencode({"payload": SAMPLE.decode("utf-8")}).encode("utf-8")
    resp = client.post(
        "/webhook/github",
        content=form_body,
        headers=_headers(
            "hard-form-1",
            form_body,
            content_type="application/x-www-form-urlencoded; charset=UTF-8",
        ),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "queued"
    assert body["pr"] == 42
    assert fake_orchestrator == [
        {"repository": REPO, "pr_number": 42, "commit_sha": OPENED_HEAD_SHA}
    ]


def test_unsupported_content_type_returns_415(fake_orchestrator):
    resp = client.post(
        "/webhook/github",
        content=SAMPLE,
        headers=_headers("hard-415-1", SAMPLE, content_type="text/plain"),
    )
    assert resp.status_code == 415
    assert fake_orchestrator == []


def test_valid_signature_but_invalid_json_returns_400(fake_orchestrator):
    body = b'{"action": "opened", '
    resp = client.post(
        "/webhook/github",
        content=body,
        headers=_headers("hard-400-1", body, content_type="application/json"),
    )
    assert resp.status_code == 400
    assert fake_orchestrator == []


def test_reserialized_body_with_original_signature_returns_401(fake_orchestrator):
    """Same JSON, different whitespace -> different bytes -> 401."""
    compact = json.dumps(json.loads(SAMPLE), separators=(",", ":")).encode("utf-8")
    assert compact != SAMPLE  # whitespace really differs
    headers = {
        "X-GitHub-Event": "pull_request",
        # Signature computed over the ORIGINAL bytes, sent with re-serialized body.
        "X-Hub-Signature-256": _sign(SAMPLE),
        "X-GitHub-Delivery": "hard-401-1",
        "Content-Type": "application/json",
    }
    resp = client.post("/webhook/github", content=compact, headers=headers)
    assert resp.status_code == 401
    assert fake_orchestrator == []


def test_dedupe_lru_stays_bounded(monkeypatch, fake_orchestrator):
    monkeypatch.setenv("WEBHOOK_DEDUPE_MAX", "3")
    for i in range(5):
        resp = client.post(
            "/webhook/github",
            content=SAMPLE,
            headers=_headers(f"hard-lru-{i}", SAMPLE),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "queued"
    assert len(fake_orchestrator) == 5
    assert len(webhook_main._seen_delivery_ids) <= 3


def test_duplicate_delivery_returns_duplicate_ignored(fake_orchestrator):
    h = _headers("hard-dupe-1", SAMPLE)
    first = client.post("/webhook/github", content=SAMPLE, headers=h)
    second = client.post("/webhook/github", content=SAMPLE, headers=h)
    assert first.json()["status"] == "queued"
    assert second.json()["status"] == "duplicate_ignored"
    assert len(fake_orchestrator) == 1


def test_redelivery_after_orchestrator_failure_succeeds(monkeypatch):
    state = {"down": True}

    async def flaky(repository, pr_number, commit_sha):
        if state["down"]:
            raise OrchestratorError("orchestrator is down")
        return {"job_id": "job-456", "status": "QUEUED", "duplicate": False}

    monkeypatch.setattr(webhook_main.orchestrator, "create_job", flaky)
    h = _headers("hard-retry-1", SAMPLE)

    first = client.post("/webhook/github", content=SAMPLE, headers=h)
    assert first.status_code == 502

    # GitHub redelivers the SAME delivery ID after the orchestrator recovers.
    state["down"] = False
    second = client.post("/webhook/github", content=SAMPLE, headers=h)
    assert second.status_code == 200
    assert second.json()["status"] == "queued"
    assert second.json()["job_id"] == "job-456"
