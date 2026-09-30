"""
Stub for handing a ReviewJobMessage off to the async pipeline.

Right now (Weeks 3-4 scope) this just logs. Once Prakrisht's
Orchestrator + Redis/Celery work lands (Weeks 5-6), replace the body
of `enqueue_review_job` with a real Celery `.delay(...)` call, e.g.:

    from celery import Celery
    celery_app = Celery("pr_reviewer", broker=settings.REDIS_URL)

    def enqueue_review_job(job: ReviewJobMessage) -> None:
        celery_app.send_task("orchestrator.tasks.start_review", args=[job.model_dump()])

Keeping this behind a single function now means the FastAPI route
code in main.py doesn't need to change later.
"""

import logging

from .schemas import ReviewJobMessage

logger = logging.getLogger("webhook.queue_client")


def enqueue_review_job(job: ReviewJobMessage) -> None:
    # TODO(Prakrisht): replace with real Celery task dispatch once
    # the Orchestrator Service + Redis broker exist.
    logger.info("Would enqueue review job: %s", job.model_dump_json())
