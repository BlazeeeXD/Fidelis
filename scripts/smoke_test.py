#!/usr/bin/env python3
"""
Smoke test for the Fidelis docker compose stack (P5, Week 1 / V0).

Standard library only. Run it against a *running* stack:

    cp .env.example .env
    docker compose up -d --build --wait
    python scripts/smoke_test.py

Steps:
  a) GET each service's health endpoint on its host port.
  b) POST gateway /auth/dev-token to mint a JWT.
  c) Create a review job through the gateway, then read it back.
  d) Deliver a signed GitHub pull_request payload through the gateway, then
     read the job back from the orchestrator (webhook → job path).
  e) Request a mock review straight from the reviewer service (it is an
     internal service, so it is reached on its host port, not via the gateway).

Prints PASS/FAIL per step and exits non-zero if anything failed.
"""

import hashlib
import hmac
import json
import os
import sys
import uuid
from pathlib import Path
from urllib import error, request

ROOT = Path(__file__).resolve().parent.parent

GATEWAY_URL = "http://localhost:8000"
ORCHESTRATOR_URL = "http://localhost:8001"
HEALTH_ENDPOINTS = [
    ("gateway /healthz", "http://localhost:8000/healthz"),
    ("orchestrator /health", "http://localhost:8001/health"),
    ("webhook /healthz", "http://localhost:8002/healthz"),
    ("reviewer /healthz", "http://localhost:8003/healthz"),
]

REVIEWER_URL = "http://localhost:8003"

SAMPLE_PAYLOAD = (
    ROOT / "services" / "webhook" / "tests" / "sample_payloads" / "pull_request_opened.json"
)

FAILURES = 0


def report(ok: bool, label: str, detail: str = "") -> None:
    global FAILURES
    tag = "PASS" if ok else "FAIL"
    line = f"[{tag}] {label}"
    if detail:
        line += f" - {detail}"
    print(line)
    if not ok:
        FAILURES += 1


def http(method: str, url: str, body=None, headers=None, timeout: float = 10.0):
    """Return (status_code_or_None, parsed_json_or_None, error_or_None)."""
    data = None
    hdrs = {"Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    if isinstance(body, bytes):
        # Raw bytes are sent verbatim — required for HMAC signatures.
        data = body
        hdrs.setdefault("Content-Type", "application/json")
    elif body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")

    req = request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except error.HTTPError as e:
        raw = e.read()
        status = e.code
    except error.URLError as e:
        return None, None, str(e.reason)
    except TimeoutError as e:
        return None, None, f"timeout: {e}"

    try:
        parsed = json.loads(raw) if raw else None
    except (ValueError, UnicodeDecodeError):
        parsed = None
    return status, parsed, None


def parse_env_file(path: Path) -> dict:
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


def webhook_secret() -> str:
    """Env var first, then .env, then .env.example (the dev placeholder)."""
    if os.environ.get("GITHUB_WEBHOOK_SECRET"):
        return os.environ["GITHUB_WEBHOOK_SECRET"]
    for name in (".env", ".env.example"):
        value = parse_env_file(ROOT / name).get("GITHUB_WEBHOOK_SECRET")
        if value:
            return value
    return "dev_webhook_secret_change_me"


def step_health() -> None:
    for label, url in HEALTH_ENDPOINTS:
        status, body, err = http("GET", url)
        if err:
            report(False, label, err)
        elif status != 200:
            report(False, label, f"HTTP {status} (expected 200)")
        else:
            report(True, label, f"HTTP 200, body={json.dumps(body)}")


def step_dev_token() -> str | None:
    status, body, err = http("POST", f"{GATEWAY_URL}/auth/dev-token")
    if err:
        report(False, "gateway POST /auth/dev-token", err)
        return None
    token = (body or {}).get("access_token") if isinstance(body, dict) else None
    ok = status == 200 and bool(token)
    report(
        ok,
        "gateway POST /auth/dev-token",
        f"HTTP {status}, access_token={'present' if token else 'missing'}",
    )
    return token if ok else None


def step_jobs(token: str) -> None:
    payload = {"repository": "test/repo", "pr_number": 1, "commit_sha": "abc123"}
    status, body, err = http(
        "POST",
        f"{GATEWAY_URL}/api/orchestrator/jobs",
        body=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    if err:
        report(False, "POST /api/orchestrator/jobs", err)
        return
    ok = status == 201 and (body or {}).get("status") == "QUEUED"
    report(
        ok,
        "POST /api/orchestrator/jobs",
        f"HTTP {status} (expected 201), status={(body or {}).get('status')!r} (expected 'QUEUED')",
    )
    if not ok:
        return

    job_id = (body or {}).get("job_id")
    status, detail, err = http(
        "GET",
        f"{GATEWAY_URL}/api/orchestrator/jobs/{job_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    if err:
        report(False, "GET /api/orchestrator/jobs/{job_id}", err)
        return
    ok = status == 200 and (detail or {}).get("job_id") == job_id
    report(
        ok,
        "GET /api/orchestrator/jobs/{job_id}",
        f"HTTP {status} (expected 200), job_id={'matches' if (detail or {}).get('job_id') == job_id else 'mismatch'}",
    )


def _expected_fields_from_sample(raw: bytes) -> dict:
    payload = json.loads(raw)
    pr = payload["pull_request"]
    return {
        "repository": payload["repository"]["full_name"],
        "pr_number": pr["number"],
        "commit_sha": pr["head"]["sha"],
    }


def step_webhook() -> None:
    raw = SAMPLE_PAYLOAD.read_bytes()
    expected = _expected_fields_from_sample(raw)
    secret = webhook_secret()
    signature = "sha256=" + hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    status, body, err = http(
        "POST",
        f"{GATEWAY_URL}/webhook/github",
        body=raw,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "pull_request",
            "X-Hub-Signature-256": signature,
            "X-GitHub-Delivery": str(uuid.uuid4()),
        },
    )
    if err:
        report(False, "POST /webhook/github (signed pull_request)", err)
        return
    ok = status == 200 and (body or {}).get("status") == "queued"
    report(
        ok,
        "POST /webhook/github (signed pull_request)",
        f"HTTP {status} (expected 200), status={(body or {}).get('status')!r} (expected 'queued')",
    )
    if not ok:
        return

    job_id = (body or {}).get("job_id")
    if not job_id:
        report(False, "webhook → orchestrator job persistence", "missing job_id in webhook response")
        return

    job_status, job_body, job_err = http("GET", f"{ORCHESTRATOR_URL}/jobs/{job_id}")
    if job_err:
        report(False, "GET orchestrator /jobs/{job_id} (after webhook)", job_err)
        return
    problems = []
    if job_status != 200:
        problems.append(f"HTTP {job_status} (expected 200)")
    if (job_body or {}).get("job_id") != job_id:
        problems.append("job_id mismatch")
    if (job_body or {}).get("status") != "QUEUED":
        problems.append(f"status={(job_body or {}).get('status')!r} (expected 'QUEUED')")
    if (job_body or {}).get("repository") != expected["repository"]:
        problems.append("repository mismatch vs sample payload")
    if (job_body or {}).get("pr_number") != expected["pr_number"]:
        problems.append("pr_number mismatch vs sample payload")
    if (job_body or {}).get("commit_sha") != expected["commit_sha"]:
        problems.append("commit_sha mismatch vs sample payload")
    report(
        not problems,
        "webhook → orchestrator job persistence",
        "fields match sample payload" if not problems else "; ".join(problems),
    )


def step_reviewer() -> None:
    """The reviewer must answer POST /review with a contract-shaped result."""
    payload = {
        "job_id": "smoke-test-job",
        "repository": "test/repo",
        "pr_number": 1,
        "commit_sha": "abc123",
    }
    status, body, err = http("POST", f"{REVIEWER_URL}/review", body=payload)
    label = "POST reviewer /review"
    if err:
        report(False, label, err)
        return
    if status != 200 or not isinstance(body, dict):
        report(False, label, f"HTTP {status} (expected 200), body={json.dumps(body)}")
        return

    findings = body.get("findings")
    summary = body.get("summary") or {}
    problems = []
    if body.get("job_id") != payload["job_id"]:
        problems.append("job_id does not echo the request")
    if not isinstance(findings, list):
        problems.append("findings is not a list")
    elif summary.get("total_findings") != len(findings):
        problems.append("summary.total_findings != len(findings)")
    for finding in findings if isinstance(findings, list) else []:
        missing = {"severity", "category", "file", "line", "message"} - set(finding)
        if missing:
            problems.append(f"finding missing {sorted(missing)}")
            break
    report(
        not problems,
        label,
        f"HTTP 200, {len(findings or [])} finding(s)" if not problems else "; ".join(problems),
    )


def main() -> int:
    print("Fidelis smoke test - stack must already be running (docker compose up -d --wait)")
    print("-" * 78)

    print("a) Health endpoints")
    step_health()

    print("b) Dev token")
    token = step_dev_token()

    print("c) Review job round-trip via gateway")
    if token:
        step_jobs(token)
    else:
        report(False, "job round-trip", "skipped: no token from step b")

    print("d) Signed GitHub webhook event via gateway")
    step_webhook()

    print("e) Mock review from the reviewer service")
    step_reviewer()

    print("-" * 78)
    if FAILURES:
        print(f"RESULT: FAIL ({FAILURES} step(s) failed)")
        return 1
    print("RESULT: PASS (all steps passed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
