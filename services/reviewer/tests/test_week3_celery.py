from unittest.mock import MagicMock

import pytest

celery = pytest.importorskip("celery")

from app.worker import ExecutionDisposition, ExecutionOutcome, JobStatus, tasks
from app.worker.celery_app import celery_app


def request_payload():
    return {
        "schema_version": "1.3",
        "job_id": "job-week3-1",
        "repository": "owner/repo",
        "pr_number": 42,
        "commit_sha": "a" * 40,
    }


def test_celery_app_uses_json_and_late_ack():
    assert celery_app.conf.task_serializer == "json"
    assert "json" in celery_app.conf.accept_content
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True
    assert celery_app.conf.worker_prefetch_multiplier == 1


def test_celery_task_returns_serialized_success_outcome(monkeypatch):
    result = {
        "job_id": "job-week3-1",
        "disposition": "SUCCEEDED",
        "job_status": "COMPLETED",
        "attempt": 1,
        "max_attempts": 3,
        "result": {"example": "mock"},
        "error": None,
        "retry_after_seconds": None,
        "duration_ms": 12,
    }

    class FakeContext:
        def __enter__(self): return self
        def __exit__(self, *args): pass

    class FakeWorker:
        def __init__(self, *args, **kwargs): pass
        def execute(self, request, *, attempt):
            assert request.job_id == "job-week3-1" and attempt == 1
            return MagicMock(model_dump=lambda mode: result, disposition=ExecutionDisposition.SUCCEEDED)

    monkeypatch.setattr(tasks, "HttpReviewerClient", lambda *a, **k: FakeContext())
    monkeypatch.setattr(tasks, "HttpJobStatusReporter", lambda *a, **k: FakeContext())
    monkeypatch.setattr(tasks, "ReviewWorker", FakeWorker)
    monkeypatch.setattr(tasks, "WORKER_STATUS_TOKEN", "secret")

    response = tasks.run_review.run(request_payload(), review_attempt=1, infrastructure_attempt=0)
    assert response == result


def test_celery_task_retries_after_worker_outcome_requests_retry(monkeypatch):
    class FakeContext:
        def __enter__(self): return self
        def __exit__(self, *args): pass

    class FakeWorker:
        def __init__(self, *args, **kwargs): pass
        def execute(self, request, *, attempt):
            return ExecutionOutcome(
                job_id=request.job_id,
                disposition=ExecutionDisposition.RETRY,
                job_status=JobStatus.QUEUED,
                attempt=attempt,
                max_attempts=3,
                error={"code": "UPSTREAM_ERROR", "message": "temporary", "retryable": True},
                retry_after_seconds=2.0,
                duration_ms=5,
            )

    monkeypatch.setattr(tasks, "HttpReviewerClient", lambda *a, **k: FakeContext())
    monkeypatch.setattr(tasks, "HttpJobStatusReporter", lambda *a, **k: FakeContext())
    monkeypatch.setattr(tasks, "ReviewWorker", FakeWorker)
    monkeypatch.setattr(tasks, "WORKER_STATUS_TOKEN", "secret")

    with pytest.raises(celery.exceptions.Retry):
        tasks.run_review.run(request_payload(), review_attempt=1, infrastructure_attempt=0)
