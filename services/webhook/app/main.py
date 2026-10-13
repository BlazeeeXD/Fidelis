"""
Webhook Service (FastAPI)

Responsibilities (per synopsis Section 3):
  - Receive GitHub webhook events
  - Validate authenticity via HMAC signature verification
  - Extract PR / repository details
  - Hand off a review job to the Orchestrator, which owns job lifecycle

Run locally (from services/webhook/):
    uvicorn app.main:app --reload --port 8002
"""

import json
import logging
import os
import uuid
from collections import OrderedDict
from typing import NoReturn
from urllib.parse import parse_qs

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import ValidationError

from .orchestrator_client import (
    OrchestratorClient,
    OrchestratorError,
    OrchestratorRejectedError,
)
from .schemas import REVIEW_TRIGGER_ACTIONS, PullRequestWebhookEvent, ReviewJobMessage
from .security import InvalidSignatureError, verify_github_signature

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("webhook.main")

GITHUB_WEBHOOK_SECRET = os.environ.get("GITHUB_WEBHOOK_SECRET", "")

DEFAULT_DEDUPE_LIMIT = 10_000

app = FastAPI(title="PR Reviewer - Webhook Service")

orchestrator = OrchestratorClient()

# In-memory de-dupe of GitHub delivery IDs for this demo/skeleton.
# GitHub retries deliveries on timeout, so without this a slow
# response could cause the same PR to be queued twice.
# (The orchestrator is also idempotent per repo/PR/commit, so this is a
# first line of defence, not the only one.)
# Bounded LRU: insertion order == least-recently-used order, so a flood
# of distinct delivery IDs can never grow this without limit.
# Size comes from WEBHOOK_DEDUPE_MAX (default 10000).
_seen_delivery_ids: OrderedDict[str, None] = OrderedDict()


def _dedupe_limit() -> int:
    """Max delivery IDs to remember, from env WEBHOOK_DEDUPE_MAX."""
    raw = os.environ.get("WEBHOOK_DEDUPE_MAX", "")
    if not raw:
        return DEFAULT_DEDUPE_LIMIT
    try:
        limit = int(raw)
    except ValueError:
        logger.warning(
            "Invalid WEBHOOK_DEDUPE_MAX=%r, using default %d", raw, DEFAULT_DEDUPE_LIMIT
        )
        return DEFAULT_DEDUPE_LIMIT
    return max(limit, 1)


def _remember_delivery(delivery_id: str) -> None:
    """Mark a delivery ID as seen, evicting the least-recently-seen one."""
    _seen_delivery_ids[delivery_id] = None
    _seen_delivery_ids.move_to_end(delivery_id)
    limit = _dedupe_limit()
    while len(_seen_delivery_ids) > limit:
        _seen_delivery_ids.popitem(last=False)


def _touch_delivery(delivery_id: str) -> None:
    """Mark a seen delivery ID as recently used."""
    _seen_delivery_ids.move_to_end(delivery_id)


def _forget_delivery(delivery_id: str) -> None:
    """Un-mark a delivery ID so a GitHub redelivery is processed again."""
    _seen_delivery_ids.pop(delivery_id, None)


def _log_delivery(
    *,
    delivery_id: str,
    event: str | None,
    outcome: str,
    action: str | None = None,
    repo: str | None = None,
    pr_number: int | None = None,
    head_sha: str | None = None,
    detail: str | None = None,
) -> None:
    """Exactly one INFO line per delivery, with a fixed field set.

    Fields we don't know yet (e.g. action on a rejected signature) are "-".
    outcome is one of: queued | duplicate | ignored | rejected.
    Never logs the secret or the request body.
    """
    logger.info(
        "delivery delivery_id=%s event=%s action=%s repo=%s pr_number=%s "
        "head_sha=%s outcome=%s detail=%s",
        delivery_id,
        event or "-",
        action if action is not None else "-",
        repo or "-",
        pr_number if pr_number is not None else "-",
        head_sha or "-",
        outcome,
        detail or "-",
    )


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
    delivery_id = x_github_delivery or str(uuid.uuid4())
    event = x_github_event

    def reject(status_code: int, http_detail: str, log_detail: str) -> NoReturn:
        """Emit the delivery's single INFO line, then fail the request."""
        _log_delivery(
            delivery_id=delivery_id,
            event=event,
            outcome="rejected",
            detail=log_detail,
        )
        raise HTTPException(status_code=status_code, detail=http_detail)

    # 1. Verify the request really came from GitHub. This ALWAYS runs on the
    #    raw body bytes first — before any parsing or content-type handling.
    if not GITHUB_WEBHOOK_SECRET:
        # Fail loudly in any real deployment; don't silently skip verification.
        logger.warning(
            "Rejected webhook delivery %s: GITHUB_WEBHOOK_SECRET not configured",
            delivery_id,
        )
        reject(500, "Server missing GITHUB_WEBHOOK_SECRET", "missing_secret")
    try:
        verify_github_signature(raw_body, GITHUB_WEBHOOK_SECRET, x_hub_signature_256)
    except InvalidSignatureError as e:
        logger.warning("Rejected webhook delivery %s: %s", delivery_id, e)
        reject(401, "Invalid signature", "invalid_signature")

    if not raw_body:
        logger.warning("Rejected webhook delivery %s: empty request body", delivery_id)
        reject(400, "Empty request body", "empty_body")

    # 2. Idempotency check (bounded LRU of delivery IDs).
    if delivery_id in _seen_delivery_ids:
        _touch_delivery(delivery_id)
        _log_delivery(delivery_id=delivery_id, event=event, outcome="duplicate")
        return {"status": "duplicate_ignored", "delivery_id": delivery_id}
    _remember_delivery(delivery_id)

    # 3. Decode the body per GitHub's delivery content type.
    #    GitHub delivers either application/json or
    #    application/x-www-form-urlencoded (body is `payload=<urlencoded json>`).
    #    A missing Content-Type is treated as JSON (GitHub always sends one;
    #    this keeps raw-bytes callers working).
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if content_type in ("", "application/json"):
        payload_bytes = raw_body
    elif content_type == "application/x-www-form-urlencoded":
        form = parse_qs(raw_body.decode("utf-8", errors="replace"), keep_blank_values=True)
        values = form.get("payload")
        if not values or not values[0]:
            reject(400, "Form body missing 'payload' field", "form_missing_payload")
        payload_bytes = values[0].encode("utf-8")
    else:
        reject(415, "Unsupported content type", f"unsupported_content_type:{content_type}")

    # 4. Parse the JSON envelope. Invalid JSON with a valid signature is a
    #    400 (the delivery is malformed), never a 500.
    try:
        data = json.loads(payload_bytes)
    except (ValueError, UnicodeDecodeError):
        reject(400, "Invalid JSON payload", "invalid_json")
    if not isinstance(data, dict):
        reject(400, "Payload must be a JSON object", "not_a_json_object")

    # 5. GitHub sends many event types (ping, issues, push, ...).
    #    We only care about pull_request events.
    if event == "ping":
        _log_delivery(
            delivery_id=delivery_id, event=event, outcome="ignored", detail="ping"
        )
        return {"status": "pong"}

    if event != "pull_request":
        _log_delivery(
            delivery_id=delivery_id, event=event, outcome="ignored", detail="non_pr_event"
        )
        return {"status": "ignored", "event": event}

    # 6. Parse ONLY the action field first. Non-trigger actions are ignored
    #    without strict validation, so a slightly different payload shape for
    #    an irrelevant action can't fail with 422.
    action = data.get("action")
    if action not in REVIEW_TRIGGER_ACTIONS:
        _log_delivery(
            delivery_id=delivery_id,
            event=event,
            action=action if isinstance(action, str) else None,
            outcome="ignored",
            detail="non_trigger_action",
        )
        return {"status": "ignored", "action": action}

    # 7. Only actions that warrant a new review go through full validation.
    try:
        payload = PullRequestWebhookEvent.model_validate(data)
    except ValidationError:
        reject(422, "Unrecognized pull_request payload shape", "validation_failed")

    # 8. Normalize and hand off to the Orchestrator.
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

    try:
        result = await orchestrator.create_job(
            repository=job.repo_full_name,
            pr_number=job.pr_number,
            commit_sha=job.head_sha,
        )
    except OrchestratorRejectedError:
        logger.exception(
            "Orchestrator rejected job for %s#%s", job.repo_full_name, job.pr_number
        )
        # Un-mark this delivery so a GitHub redelivery isn't dropped as a
        # duplicate (the failure is our bug, GitHub should resend).
        _forget_delivery(delivery_id)
        _log_delivery(
            delivery_id=delivery_id,
            event=event,
            action=job.action,
            repo=job.repo_full_name,
            pr_number=job.pr_number,
            head_sha=job.head_sha,
            outcome="rejected",
            detail="orchestrator_rejected",
        )
        raise HTTPException(status_code=500, detail="Failed to create review job")
    except OrchestratorError:
        logger.exception(
            "Orchestrator unavailable for %s#%s", job.repo_full_name, job.pr_number
        )
        # Un-mark this delivery so a GitHub redelivery isn't dropped as a duplicate.
        _forget_delivery(delivery_id)
        _log_delivery(
            delivery_id=delivery_id,
            event=event,
            action=job.action,
            repo=job.repo_full_name,
            pr_number=job.pr_number,
            head_sha=job.head_sha,
            outcome="rejected",
            detail="orchestrator_unavailable",
        )
        raise HTTPException(status_code=502, detail="Orchestrator unavailable")

    _log_delivery(
        delivery_id=delivery_id,
        event=event,
        action=job.action,
        repo=job.repo_full_name,
        pr_number=job.pr_number,
        head_sha=job.head_sha,
        outcome="queued",
    )
    return {
        "status": "queued",
        "pr": job.pr_number,
        "repo": job.repo_full_name,
        "job_id": result["job_id"],
        "duplicate": result.get("duplicate", False),
    }
