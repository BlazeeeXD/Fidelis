"""Integration tests: signed GitHub webhook -> orchestrator job.

Run against a *running* compose stack (see README, "Integration tests
(webhook -> job)"):

    docker compose up -d --build --wait
    pytest tests/integration -v

All tests are skipped automatically when the stack is down.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
GATEWAY_URL = "http://localhost:8000"
ORCHESTRATOR_URL = "http://localhost:8001"
WEBHOOK_URL = "http://localhost:8002"

SAMPLE_PAYLOAD = (
    ROOT / "services" / "webhook" / "tests" / "sample_payloads" / "pull_request_opened.json"
)
TIMEOUT = 5.0


def _parse_env_file(path: Path) -> dict:
    values = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def _webhook_secret() -> str:
    """Same resolution order as scripts/smoke_test.py: env, .env, .env.example."""
    if os.environ.get("GITHUB_WEBHOOK_SECRET"):
        return os.environ["GITHUB_WEBHOOK_SECRET"]
    for name in (".env", ".env.example"):
        value = _parse_env_file(ROOT / name).get("GITHUB_WEBHOOK_SECRET")
        if value:
            return value
    return "dev_webhook_secret_change_me"


def _stack_up() -> bool:
    for url in (f"{GATEWAY_URL}/healthz", f"{ORCHESTRATOR_URL}/health"):
        try:
            if httpx.get(url, timeout=2.0).status_code != 200:
                return False
        except httpx.HTTPError:
            return False
    return True


pytestmark = pytest.mark.skipif(
    not _stack_up(),
    reason="compose stack is not running (docker compose up -d --build --wait)",
)

SAMPLE = SAMPLE_PAYLOAD.read_bytes()
_SAMPLE = json.loads(SAMPLE)
REPO = _SAMPLE["repository"]["full_name"]
PR_NUMBER = _SAMPLE["pull_request"]["number"]
HEAD_SHA = _SAMPLE["pull_request"]["head"]["sha"]


def _headers(delivery: str) -> dict:
    signature = "sha256=" + hmac.new(
        _webhook_secret().encode(), SAMPLE, hashlib.sha256
    ).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-GitHub-Event": "pull_request",
        "X-Hub-Signature-256": signature,
        "X-GitHub-Delivery": delivery,
    }


def _deliver(url: str, delivery: str) -> dict:
    """POST a signed pull_request delivery; assert 200, return the body."""
    resp = httpx.post(url, content=SAMPLE, headers=_headers(delivery), timeout=TIMEOUT)
    assert resp.status_code == 200, f"{url} -> HTTP {resp.status_code}: {resp.text}"
    return resp.json()


def _get_job(job_id: str) -> dict:
    resp = httpx.get(f"{ORCHESTRATOR_URL}/jobs/{job_id}", timeout=TIMEOUT)
    assert resp.status_code == 200, f"GET /jobs/{job_id} -> HTTP {resp.status_code}"
    return resp.json()


def test_gateway_webhook_creates_queued_job():
    """Signed delivery via the gateway -> QUEUED job readable from orchestrator."""
    body = _deliver(f"{GATEWAY_URL}/webhook/github", str(uuid.uuid4()))
    assert body["status"] == "queued"

    job = _get_job(body["job_id"])
    assert job["job_id"] == body["job_id"]
    assert job["status"] == "QUEUED"
    assert job["repository"] == REPO
    assert job["pr_number"] == PR_NUMBER
    assert job["commit_sha"] == HEAD_SHA


def test_direct_webhook_path_creates_queued_job():
    """The webhook service is also reachable directly on its host port."""
    body = _deliver(f"{WEBHOOK_URL}/webhook/github", str(uuid.uuid4()))
    assert body["status"] == "queued"

    job = _get_job(body["job_id"])
    assert job["status"] == "QUEUED"
    assert job["repository"] == REPO
    assert job["pr_number"] == PR_NUMBER
    assert job["commit_sha"] == HEAD_SHA


def test_duplicate_delivery_id_is_ignored():
    """A GitHub redelivery of the same X-GitHub-Delivery is processed once."""
    delivery = str(uuid.uuid4())
    first = _deliver(f"{GATEWAY_URL}/webhook/github", delivery)
    assert first["status"] == "queued"

    resp = httpx.post(
        f"{GATEWAY_URL}/webhook/github", content=SAMPLE, headers=_headers(delivery), timeout=TIMEOUT
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "duplicate_ignored"


def test_orchestrator_idempotency_returns_same_job():
    """POST /jobs is idempotent on (repository, pr_number, commit_sha)."""
    payload = {"repository": REPO, "pr_number": PR_NUMBER, "commit_sha": HEAD_SHA}
    first = httpx.post(f"{ORCHESTRATOR_URL}/jobs", json=payload, timeout=TIMEOUT)
    assert first.status_code in (200, 201), first.text

    second = httpx.post(f"{ORCHESTRATOR_URL}/jobs", json=payload, timeout=TIMEOUT)
    assert second.status_code in (200, 201), second.text

    first_body, second_body = first.json(), second.json()
    assert second_body["job_id"] == first_body["job_id"]
    assert second_body["duplicate"] is True
