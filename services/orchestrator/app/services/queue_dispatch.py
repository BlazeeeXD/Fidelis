"""Reliable hand-off from persisted jobs to the Celery broker.

The job and its outbox row are committed together by OrchestratorService.
Publishing is at-least-once: the outbox row is locked while a message is sent,
then marked published. If publishing fails, the transaction rolls back and a
webhook redelivery can safely try again.
"""

import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import select

from app.db.database import SessionLocal
from app.db.models import ReviewJobDB, ReviewJobOutboxDB
from app.models.job import JobStatus

logger = logging.getLogger("orchestrator.queue")

BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0")
_celery_producer = None
TASK_NAME = "fidelis.run_review"


def _get_producer():
    """Import Celery only when async dispatch is actually used."""
    global _celery_producer
    if _celery_producer is None:
        from celery import Celery

        _celery_producer = Celery("fidelis-orchestrator", broker=BROKER_URL)
        _celery_producer.conf.update(
            task_serializer="json",
            accept_content=["json"],
            result_serializer="json",
            broker_connection_retry_on_startup=True,
            task_default_queue="review-jobs",
        )
    return _celery_producer


def _publish(payload: dict, task_id: str) -> None:
    """Publish one review task. Kept separate for deterministic unit tests."""
    _get_producer().send_task(
        TASK_NAME,
        kwargs={"request_payload": payload, "review_attempt": 1},
        task_id=task_id,
        queue="review-jobs",
    )


def dispatch_pending_job(
    job_id: str,
    *,
    session_factory: Callable = SessionLocal,
    publisher: Callable[[dict, str], None] | None = None,
) -> bool:
    """Publish a job's pending outbox row once; return whether it was published.

    PostgreSQL's row lock serializes concurrent dispatch attempts for the same
    job. The outbox row remains pending if Redis/Celery publication raises.
    """
    publisher = publisher or _publish
    db = session_factory()
    try:
        outbox = db.execute(
            select(ReviewJobOutboxDB)
            .where(ReviewJobOutboxDB.job_id == job_id)
            .with_for_update()
        ).scalar_one_or_none()
        if outbox is None or outbox.published_at is not None:
            db.rollback()
            return False

        job = db.get(ReviewJobDB, job_id)
        if job is None:
            db.rollback()
            raise LookupError(f"outbox references unknown review job {job_id}")

        # Only newly accepted work should enter the queue. A terminal or
        # already-running record must never be dispatched by a duplicate PR.
        if job.status is not JobStatus.QUEUED:
            outbox.published_at = datetime.now(UTC)
            db.commit()
            return False

        payload = {
            "schema_version": "1.3",
            "job_id": job.id,
            "repository": job.repository,
            "pr_number": job.pr_number,
            "commit_sha": job.commit_sha,
        }
        publisher(payload, job.id)
        outbox.published_at = datetime.now(UTC)
        db.commit()
        logger.info("published review job %s to Celery", job_id)
        return True
    except Exception:
        db.rollback()
        logger.exception("could not publish review job %s; outbox remains pending", job_id)
        raise
    finally:
        db.close()


def dispatch_pending_jobs(
    *,
    limit: int = 50,
    session_factory: Callable = SessionLocal,
    publisher: Callable[[dict, str], None] | None = None,
) -> int:
    """Sweep the durable outbox and publish a bounded batch of pending jobs.

    This closes the crash window where the API commits a job but the broker is
    unavailable before its first publish. The next sweep retries without
    requiring GitHub to redeliver the webhook.
    """
    db = session_factory()
    try:
        pending_ids = db.execute(
            select(ReviewJobOutboxDB.job_id)
            .join(ReviewJobDB, ReviewJobDB.id == ReviewJobOutboxDB.job_id)
            .where(
                ReviewJobOutboxDB.published_at.is_(None),
                ReviewJobDB.status == JobStatus.QUEUED,
            )
            .order_by(ReviewJobOutboxDB.created_at.asc())
            .limit(max(1, min(int(limit), 500)))
        ).scalars().all()
    finally:
        db.close()

    published = 0
    for job_id in pending_ids:
        try:
            if dispatch_pending_job(
                job_id, session_factory=session_factory, publisher=publisher
            ):
                published += 1
        except Exception:
            # One poisoned/unavailable item must not starve later outbox rows.
            logger.exception("outbox sweep could not publish job %s", job_id)
    return published
