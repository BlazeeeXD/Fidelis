"""Celery app for repository-learning jobs."""
import os
from celery import Celery

celery_app = Celery(
    "fidelis-learner",
    broker=os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0"),
    backend=os.getenv("CELERY_RESULT_BACKEND", "redis://redis:6379/1"),
    include=["app.tasks"],
)
celery_app.conf.update(
    task_default_queue="learning-jobs",
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    result_expires=86400,
    task_track_started=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
)
