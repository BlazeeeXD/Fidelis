"""Celery task adapter around the review-service's tested ReviewWorker."""

import asyncio
import logging
import os

from app.github_reviews import GitHubReviewPublisher
from app.schemas import ReviewRequest
from app.worker.celery_app import celery_app
from app.worker.clients import HttpReviewerClient
from app.worker.interface import ExecutionDisposition
from app.worker.reporters import (
    HttpJobStatusReporter,
    StatusReporterUnavailable,
)
from app.worker.runner import ReviewWorker

logger = logging.getLogger("reviewer.worker.celery")

REVIEWER_URL = os.getenv("REVIEWER_SERVICE_URL", "http://reviewer:8000")
ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://orchestrator:8000")
WORKER_STATUS_TOKEN = os.getenv("WORKER_STATUS_TOKEN", "")
MAX_REVIEW_ATTEMPTS = int(os.getenv("REVIEW_MAX_ATTEMPTS", "3"))
REVIEWER_TIMEOUT_SECONDS = float(os.getenv("REVIEWER_TIMEOUT_SECONDS", "1400"))
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_API_URL = os.getenv("GITHUB_API_URL", "https://api.github.com")
PUBLISH_GITHUB_REVIEWS = os.getenv("PUBLISH_GITHUB_REVIEWS", "true").strip().lower() in {"1", "true", "yes", "on"}
INFRASTRUCTURE_RETRY_LIMIT = 8


@celery_app.task(
    bind=True,
    name="fidelis.run_review",
    max_retries=None,
    acks_late=True,
    reject_on_worker_lost=True,
)
def run_review(
    self,
    request_payload: dict,
    review_attempt: int = 1,
    infrastructure_attempt: int = 0,
) -> dict:
    """Execute one review attempt and translate its outcome into Celery behavior.

    Reviewer retry policy belongs to ReviewWorker. Infrastructure retries (a
    temporary failure reporting state to the Orchestrator) are tracked
    separately so they do not consume the review attempt budget.
    """
    request = ReviewRequest.model_validate(request_payload)
    if not WORKER_STATUS_TOKEN:
        raise RuntimeError("WORKER_STATUS_TOKEN is not configured for the worker")

    with (
        HttpReviewerClient(REVIEWER_URL, timeout=REVIEWER_TIMEOUT_SECONDS) as reviewer,
        HttpJobStatusReporter(ORCHESTRATOR_URL, token=WORKER_STATUS_TOKEN) as reporter,
    ):
        worker = ReviewWorker(
            reviewer,
            reporter,
            max_attempts=MAX_REVIEW_ATTEMPTS,
        )
        try:
            outcome = worker.execute(request, attempt=review_attempt)
            if (
                outcome.disposition is ExecutionDisposition.SUCCEEDED
                and outcome.result is not None
                and PUBLISH_GITHUB_REVIEWS
            ):
                try:
                    delivery = asyncio.run(
                        GitHubReviewPublisher(GITHUB_TOKEN, api_url=GITHUB_API_URL).publish(
                            request, outcome.result
                        )
                    )
                    logger.info("job %s GitHub delivery: %s", request.job_id, delivery)
                    reporter.record_delivery(
                        request.job_id,
                        status="published" if delivery.get("published") else "skipped",
                        message=str(delivery.get("reason", "")),
                        metadata={"comment_id": delivery.get("comment_id"), "html_url": delivery.get("html_url"), "comment_url": delivery.get("comment_url"), "action": delivery.get("action"), "inline_comments": 0},
                    )
                except Exception as exc:
                    # A GitHub comment outage must not erase a successfully
                    # completed and durably stored code review.
                    logger.exception("job %s: GitHub review delivery failed", request.job_id)
                    try:
                        reporter.record_delivery(request.job_id, status="failed", message=str(exc)[:500])
                    except Exception:
                        logger.exception("job %s: could not record GitHub delivery failure", request.job_id)
        except StatusReporterUnavailable as exc:
            if infrastructure_attempt >= INFRASTRUCTURE_RETRY_LIMIT:
                logger.exception(
                    "job %s: status reporting still unavailable after %s retries",
                    request.job_id,
                    infrastructure_attempt,
                )
                raise
            delay = min(30, 2 ** infrastructure_attempt)
            logger.warning(
                "job %s: retrying status report in %ss (%s/%s)",
                request.job_id,
                delay,
                infrastructure_attempt + 1,
                INFRASTRUCTURE_RETRY_LIMIT,
            )
            raise self.retry(
                exc=exc,
                countdown=delay,
                kwargs={
                    "request_payload": request_payload,
                    "review_attempt": review_attempt,
                    "infrastructure_attempt": infrastructure_attempt + 1,
                },
            )

    if outcome.disposition is ExecutionDisposition.RETRY:
        # ReviewWorker already moved RUNNING -> QUEUED and selected the
        # exponential-backoff delay. Celery republishes the task for attempt+1.
        logger.warning(
            "job %s: reviewer requested retry after attempt %s/%s in %.1fs",
            outcome.job_id,
            outcome.attempt,
            outcome.max_attempts,
            outcome.retry_after_seconds or 0.0,
        )
        raise self.retry(
            countdown=outcome.retry_after_seconds,
            kwargs={
                "request_payload": request_payload,
                "review_attempt": review_attempt + 1,
                "infrastructure_attempt": 0,
            },
        )

    return outcome.model_dump(mode="json")
