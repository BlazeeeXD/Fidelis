"""
Webhook Service (FastAPI)

Responsibilities (per synopsis Section 3):
  - Receive GitHub webhook events
  - Validate authenticity via HMAC signature verification
  - Extract PR / repository details
  - Hand off a normalized review job to the async pipeline

Run locally:
    uvicorn webhook.main:app --reload --port 8001
"""

import logging
import os
import uuid

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import ValidationError

from .queue_client import enqueue_review_job
from .schemas import REVIEW_TRIGGER_ACTIONS, PullRequestWebhookEvent, ReviewJobMessage
from .security import InvalidSignatureError, verify_github_signature

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("webhook.main")

GITHUB_WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")

app = FastAPI(title="PR Reviewer - Webhook Service")

# In-memory de-dupe of GitHub delivery IDs for this demo/skeleton.
# GitHub retries deliveries on timeout, so without this a slow
# response could cause the same PR to be queued twice.
# Replace with a Redis SETNX check once Redis is wired up (Weeks 5-6).
_seen_delivery_ids: set[str] = set()


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.post("/webhook/github")
async def github_webhook(
    request: Request,
    x_github_event: str | None = Header(default=None, alias="X-GitHub-Event"),
    x_hub_signature_256: str | None = Header(default=None, alias="X-Hub-Signature-256"),
    x_github_delivery: str | None = Header(default=None, alias="X-GitHub-Delivery"),
):
    raw_body = await request.body()

    # 1. Verify the request really came from GitHub.
    if not GITHUB_WEBHOOK_SECRET:
        # Fail loudly in any real deployment; don't silently skip verification.
        raise HTTPException(status_code=500, detail="Server missing GITHUB_WEBHOOK_SECRET")
    try:
        verify_github_signature(raw_body, GITHUB_WEBHOOK_SECRET, x_hub_signature_256)
    except InvalidSignatureError as e:
        logger.warning("Rejected webhook: %s", e)
        raise HTTPException(status_code=401, detail="Invalid signature")

    # 2. Idempotency check.
    delivery_id = x_github_delivery or str(uuid.uuid4())
    if delivery_id in _seen_delivery_ids:
        return {"status": "duplicate_ignored", "delivery_id": delivery_id}
    _seen_delivery_ids.add(delivery_id)

    # 3. GitHub sends many event types (ping, issues, push, ...).
    #    We only care about pull_request events.
    if x_github_event == "ping":
        return {"status": "pong"}

    if x_github_event != "pull_request":
        logger.info("Ignoring non-PR event: %s", x_github_event)
        return {"status": "ignored", "event": x_github_event}

    # 4. Parse and validate payload shape.
    try:
        payload = PullRequestWebhookEvent.model_validate_json(raw_body)
    except ValidationError as e:
        logger.error("Payload validation failed: %s", e)
        raise HTTPException(status_code=422, detail="Unrecognized pull_request payload shape")

    # 5. Only react to actions that actually warrant a new review.
    if payload.action not in REVIEW_TRIGGER_ACTIONS:
        return {"status": "ignored", "action": payload.action}

    # 6. Normalize and enqueue.
    job = ReviewJobMessage(
        event_id=delivery_id,
        action=payload.action,
        repo_full_name=payload.repository.full_name,
        pr_number=payload.pull_request.number,
        pr_title=payload.pull_request.title,
        head_sha=payload.pull_request.head.sha,
        base_sha=payload.pull_request.base.sha,
        diff_url=payload.pull_request.diff_url,
        html_url=payload.pull_request.html_url,
        author_login=payload.pull_request.user.login,
    )
    enqueue_review_job(job)

    return {"status": "queued", "pr": job.pr_number, "repo": job.repo_full_name}
