from datetime import UTC, datetime

import pytest
from app.db import models  # noqa: F401
from app.db.database import Base
from app.db.models import ReviewAgentRunDB, ReviewJobDB, ReviewJobOutboxDB
from app.main import app
from app.models.job import JobStatus
from app.services.orchestrator import InvalidJobTransitionError, orchestrator_service
from app.services.queue_dispatch import dispatch_pending_job, dispatch_pending_jobs
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.fixture(autouse=True)
def configure_test_database(monkeypatch):
    """Give every test its own in-memory SQLite database."""
    monkeypatch.setenv("ASYNC_REVIEW_ENABLED", "false")
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    Base.metadata.create_all(bind=engine)

    TestSessionLocal = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
    )

    monkeypatch.setattr(
        orchestrator_service,
        "_session_factory",
        TestSessionLocal,
    )

    yield TestSessionLocal  # tests can request this fixture to touch the DB directly

    engine.dispose()


client = TestClient(app)

PAYLOAD = {"repository": "test/repo", "pr_number": 1, "commit_sha": "abc123"}


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


def test_create_job():
    response = client.post(
        "/jobs",
        json={
            "repository": "test/repo",
            "pr_number": 1,
            "commit_sha": "abc123",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert "job_id" in body
    assert body["status"] == "QUEUED"


def test_retrieve_job():
    create = client.post(
        "/jobs",
        json={
            "repository": "test/repo",
            "pr_number": 2,
            "commit_sha": "def456",
        },
    )

    assert create.status_code == 201
    job_id = create.json()["job_id"]

    response = client.get(f"/jobs/{job_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["job_id"] == job_id
    assert body["repository"] == "test/repo"
    assert body["pr_number"] == 2
    assert body["commit_sha"] == "def456"
    assert body["status"] == "QUEUED"


def test_retrieve_nonexistent_job():
    response = client.get("/jobs/random-nonexistent-id")
    assert response.status_code == 404


# ---------------------------- Week 2 additions ----------------------------


def test_new_job_not_marked_duplicate():
    assert client.post("/jobs", json=PAYLOAD).json()["duplicate"] is False


def test_duplicate_returns_existing_job():
    first = client.post("/jobs", json=PAYLOAD)
    second = client.post("/jobs", json=PAYLOAD)

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["job_id"] == first.json()["job_id"]
    assert second.json()["duplicate"] is True
    assert len(client.get("/jobs").json()) == 1


def test_database_index_blocks_duplicate_active_jobs(configure_test_database):
    """Idempotency must also hold across orchestrator processes."""
    client.post("/jobs", json=PAYLOAD)
    db = configure_test_database()
    try:
        duplicate = ReviewJobDB(
            id="duplicate-active-job",
            repository=PAYLOAD["repository"],
            pr_number=PAYLOAD["pr_number"],
            commit_sha=PAYLOAD["commit_sha"],
            status=JobStatus.QUEUED,
            created_at=datetime.now(UTC),
            started_at=None,
            completed_at=None,
            error_message=None,
        )
        db.add(duplicate)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
    finally:
        db.close()


def test_startup_recreates_missing_idempotency_index(monkeypatch, configure_test_database):
    """Existing named volumes also receive indexes added in later versions."""
    engine = configure_test_database.kw["bind"]
    index = next(iter(ReviewJobDB.__table__.indexes))
    index.drop(bind=engine, checkfirst=True)
    monkeypatch.setattr("app.main.engine", engine)

    with TestClient(app):
        pass

    index_names = {entry["name"] for entry in inspect(engine).get_indexes("review_jobs")}
    assert index.name in index_names


def test_new_commit_creates_new_job():
    a = client.post("/jobs", json=PAYLOAD)
    b = client.post("/jobs", json={**PAYLOAD, "commit_sha": "def456"})

    assert b.status_code == 201
    assert a.json()["job_id"] != b.json()["job_id"]


def test_failed_job_does_not_block_retry(configure_test_database):
    first = client.post("/jobs", json=PAYLOAD).json()

    db = configure_test_database()
    row = db.get(ReviewJobDB, first["job_id"])
    row.status = JobStatus.FAILED
    db.commit()
    db.close()

    second = client.post("/jobs", json=PAYLOAD)
    assert second.status_code == 201
    assert second.json()["job_id"] != first["job_id"]


def test_validation_errors():
    assert client.post("/jobs", json={**PAYLOAD, "repository": "norepo"}).status_code == 422
    assert client.post("/jobs", json={**PAYLOAD, "pr_number": 0}).status_code == 422
    assert client.post("/jobs", json={"repository": "a/b", "pr_number": 1}).status_code == 422


def test_list_filters():
    client.post("/jobs", json=PAYLOAD)
    client.post("/jobs", json={**PAYLOAD, "pr_number": 2})
    client.post("/jobs", json={**PAYLOAD, "repository": "other/repo"})

    assert len(client.get("/jobs").json()) == 3
    assert len(client.get("/jobs", params={"repository": "test/repo"}).json()) == 2
    assert len(client.get("/jobs", params={"pr_number": 2}).json()) == 1
    assert len(client.get("/jobs", params={"status": "RUNNING"}).json()) == 0


def test_list_is_newest_first():
    older = client.post("/jobs", json=PAYLOAD).json()["job_id"]
    newer = client.post("/jobs", json={**PAYLOAD, "pr_number": 2}).json()["job_id"]

    ids = [j["job_id"] for j in client.get("/jobs").json()]
    assert ids == [newer, older]

# ---------------------------- Week 3 additions ----------------------------

def test_new_job_creates_pending_outbox_row(configure_test_database):
    created = client.post("/jobs", json=PAYLOAD)
    assert created.status_code == 201
    db = configure_test_database()
    try:
        outbox = db.get(ReviewJobOutboxDB, created.json()["job_id"])
        assert outbox is not None
        assert outbox.published_at is None
    finally:
        db.close()


def test_outbox_publishes_once_and_records_success(configure_test_database):
    created = client.post("/jobs", json={**PAYLOAD, "commit_sha": "outbox-abc"})
    job_id = created.json()["job_id"]
    sent = []

    def publisher(payload, task_id):
        sent.append((payload, task_id))

    assert dispatch_pending_job(job_id, session_factory=configure_test_database, publisher=publisher) is True
    assert dispatch_pending_job(job_id, session_factory=configure_test_database, publisher=publisher) is False
    assert len(sent) == 1
    assert sent[0][1] == job_id
    assert sent[0][0]["commit_sha"] == "outbox-abc"

    db = configure_test_database()
    try:
        assert db.get(ReviewJobOutboxDB, job_id).published_at is not None
    finally:
        db.close()


def test_outbox_sweep_recovers_pending_rows_without_webhook_redelivery(configure_test_database):
    first = client.post("/jobs", json={**PAYLOAD, "commit_sha": "sweep-a"}).json()["job_id"]
    second = client.post("/jobs", json={**PAYLOAD, "commit_sha": "sweep-b"}).json()["job_id"]
    sent = []
    count = dispatch_pending_jobs(
        session_factory=configure_test_database,
        publisher=lambda payload, task_id: sent.append(task_id),
    )
    assert count == 2
    assert set(sent) == {first, second}
    assert dispatch_pending_jobs(
        session_factory=configure_test_database, publisher=lambda payload, task_id: sent.append(task_id)
    ) == 0


def test_failed_publish_leaves_outbox_pending_for_redelivery(configure_test_database):
    created = client.post("/jobs", json={**PAYLOAD, "commit_sha": "outbox-retry"})
    job_id = created.json()["job_id"]

    def broken_publisher(payload, task_id):
        raise ConnectionError("Redis unavailable")

    with pytest.raises(ConnectionError):
        dispatch_pending_job(job_id, session_factory=configure_test_database, publisher=broken_publisher)

    db = configure_test_database()
    try:
        assert db.get(ReviewJobOutboxDB, job_id).published_at is None
    finally:
        db.close()

    sent = []
    assert dispatch_pending_job(
        job_id, session_factory=configure_test_database, publisher=lambda payload, task_id: sent.append(task_id)
    ) is True
    assert sent == [job_id]


def test_worker_status_lifecycle_and_terminal_guard(monkeypatch):
    monkeypatch.setenv("WORKER_STATUS_TOKEN", "week3-test-token")
    created = client.post("/jobs", json={**PAYLOAD, "commit_sha": "status-flow"})
    job_id = created.json()["job_id"]
    headers = {"X-Worker-Token": "week3-test-token"}

    started = client.post(f"/jobs/{job_id}/status", json={"event": "started", "attempt": 1}, headers=headers)
    assert started.status_code == 200
    first_started_at = started.json()["started_at"]
    assert started.json()["status"] == "RUNNING"

    retry = client.post(
        f"/jobs/{job_id}/status",
        json={"event": "retry_scheduled", "attempt": 1, "error_message": "temporary", "retry_after_seconds": 2},
        headers=headers,
    )
    assert retry.status_code == 200 and retry.json()["status"] == "QUEUED"
    assert retry.json()["started_at"] == first_started_at
    retry_event = client.get(f"/jobs/{job_id}/events").json()[-1]
    assert retry_event["metadata"]["retry_after_seconds"] == 2

    restarted = client.post(f"/jobs/{job_id}/status", json={"event": "started", "attempt": 2}, headers=headers)
    assert restarted.status_code == 200 and restarted.json()["status"] == "RUNNING"
    assert restarted.json()["started_at"] == first_started_at

    completed_result = _valid_result(job_id)
    completed_result["commit_sha"] = "status-flow"
    completed = client.post(
        f"/jobs/{job_id}/status",
        json={"event": "completed", "result": completed_result},
        headers=headers,
    )
    assert completed.status_code == 200
    assert completed.json()["status"] == "COMPLETED"
    assert completed.json()["completed_at"] is not None
    assert completed.json()["error_message"] is None

    rejected = client.post(f"/jobs/{job_id}/status", json={"event": "started", "attempt": 3}, headers=headers)
    assert rejected.status_code == 409


def test_worker_status_endpoint_requires_internal_token(monkeypatch):
    monkeypatch.setenv("WORKER_STATUS_TOKEN", "week3-test-token")
    created = client.post("/jobs", json={**PAYLOAD, "commit_sha": "auth-status"})
    job_id = created.json()["job_id"]
    response = client.post(f"/jobs/{job_id}/status", json={"event": "started"})
    assert response.status_code == 401


def test_worker_status_failure_is_terminal(monkeypatch):
    monkeypatch.setenv("WORKER_STATUS_TOKEN", "week3-test-token")
    created = client.post("/jobs", json={**PAYLOAD, "commit_sha": "status-failed"})
    job_id = created.json()["job_id"]
    headers = {"X-Worker-Token": "week3-test-token"}
    started = client.post(f"/jobs/{job_id}/status", json={"event": "started", "attempt": 1}, headers=headers)
    assert started.status_code == 200
    failed = client.post(
        f"/jobs/{job_id}/status",
        json={"event": "failed", "attempt": 1, "error_message": "reviewer unavailable"},
        headers=headers,
    )
    assert failed.status_code == 200
    assert failed.json()["status"] == "FAILED"
    assert failed.json()["completed_at"] is not None
    assert failed.json()["error_message"] == "reviewer unavailable"
    assert client.post(
        f"/jobs/{job_id}/status", json={"event": "started", "attempt": 2}, headers=headers
    ).status_code == 409


def _valid_result(job_id: str) -> dict:
    return {
        "schema_version": "1.3",
        "job_id": job_id,
        "repository": "test/repo",
        "pr_number": 1,
        "commit_sha": "abc123",
        "reviewer": {"name": "test-reviewer", "version": "1.0", "mode": "MOCK"},
        "agents_run": ["GENERAL"],
        "agent_runs": [{"agent": "GENERAL", "duration_ms": 800, "findings_count": 1, "input_tokens": 120, "output_tokens": 45, "model": "test-model"}],
        "findings": [{
            "id": "finding-1", "agent": "GENERAL", "severity": "HIGH", "category": "BUG",
            "file": "app.py", "line": 10, "end_line": None,
            "message": "A test bug was found.", "suggestion": "Fix it.", "confidence": 0.9,
        }],
        "summary": {
            "total_findings": 1,
            "by_severity": {"HIGH": 1, "MEDIUM": 0, "LOW": 0},
            "by_category": {"BUG": 1, "SECURITY": 0, "ARCHITECTURE": 0, "MAINTAINABILITY": 0, "STYLE": 0},
            "by_agent": {"GENERAL": 1, "STATIC_ANALYSIS": 0, "SECURITY": 0, "ARCHITECTURE": 0, "CODE_STYLE": 0},
            "highest_severity": "HIGH",
        },
        "started_at": "2026-11-11T10:00:00+00:00",
        "completed_at": "2026-11-11T10:00:02+00:00",
        "duration_ms": 2000,
    }


def test_completed_status_persists_result_findings_and_metrics(monkeypatch):
    monkeypatch.setenv("WORKER_STATUS_TOKEN", "result-token")
    created = client.post("/jobs", json=PAYLOAD).json()
    job_id = created["job_id"]
    headers = {"X-Worker-Token": "result-token"}
    assert client.post(f"/jobs/{job_id}/status", json={"event": "started", "attempt": 1}, headers=headers).status_code == 200
    completed = client.post(
        f"/jobs/{job_id}/status",
        json={"event": "completed", "result": _valid_result(job_id)},
        headers=headers,
    )
    assert completed.status_code == 200
    assert completed.json()["status"] == "COMPLETED"
    assert client.get(f"/jobs/{job_id}/result").json()["findings"][0]["severity"] == "HIGH"
    events = client.get(f"/jobs/{job_id}/events").json()
    assert [event["event"] for event in events] == ["started", "completed"]
    metrics = client.get("/metrics/summary").json()
    assert metrics["completed_jobs"] == 1
    assert metrics["total_findings"] == 1
    assert metrics["by_severity"]["HIGH"] == 1
    assert metrics["average_agent_duration_ms"]["GENERAL"] == 800
    assert metrics["slowest_agent"] == "GENERAL"
    assert metrics["total_input_tokens"] == 120
    assert metrics["total_output_tokens"] == 45


def test_single_pass_metrics_count_pipeline_tokens_once_and_hide_shared_agent_timings(monkeypatch):
    monkeypatch.setenv("WORKER_STATUS_TOKEN", "result-token")
    job_id = client.post("/jobs", json=PAYLOAD).json()["job_id"]
    headers = {"X-Worker-Token": "result-token"}
    assert client.post(f"/jobs/{job_id}/status", json={"event": "started", "attempt": 1}, headers=headers).status_code == 200
    result = _valid_result(job_id)
    result["agents_run"] = ["STATIC_ANALYSIS", "SECURITY", "ARCHITECTURE", "CODE_STYLE"]
    result["agent_runs"] = [
        {"agent": agent, "duration_ms": 0, "findings_count": 0, "shared_call": True}
        for agent in result["agents_run"]
    ]
    result["execution"] = {
        "strategy": "single_pass", "llm_requests": 1,
        "input_tokens": 4200, "output_tokens": 700,
        "duration_ms": 12000, "checkpoints_reused": 0,
        "selected_agents": result["agents_run"],
    }
    response = client.post(
        f"/jobs/{job_id}/status",
        json={"event": "completed", "result": result},
        headers=headers,
    )
    assert response.status_code == 200
    metrics = client.get("/metrics/summary").json()
    assert metrics["total_input_tokens"] == 4200
    assert metrics["total_output_tokens"] == 700
    assert metrics["average_agent_duration_ms"] == {}
    assert metrics["slowest_agent"] is None


def test_result_identity_mismatch_is_rejected(monkeypatch):
    monkeypatch.setenv("WORKER_STATUS_TOKEN", "result-token")
    job_id = client.post("/jobs", json=PAYLOAD).json()["job_id"]
    headers = {"X-Worker-Token": "result-token"}
    client.post(f"/jobs/{job_id}/status", json={"event": "started"}, headers=headers)
    result = _valid_result(job_id)
    result["commit_sha"] = "someone-elses-commit"
    response = client.post(f"/jobs/{job_id}/status", json={"event": "completed", "result": result}, headers=headers)
    assert response.status_code == 422


def test_replay_creates_new_job_after_completed_original(configure_test_database):
    first = client.post("/jobs", json=PAYLOAD).json()
    db = configure_test_database()
    row = db.get(ReviewJobDB, first["job_id"])
    row.status = JobStatus.COMPLETED
    row.completed_at = datetime.now(UTC)
    db.commit()
    db.close()
    replay = client.post(f"/jobs/{first['job_id']}/replay")
    assert replay.status_code == 201
    assert replay.json()["job_id"] != first["job_id"]
    assert len(client.get("/jobs").json()) == 2


def test_repository_rules_round_trip(monkeypatch):
    monkeypatch.setenv("WORKER_STATUS_TOKEN", "rules-token")
    headers = {"X-Worker-Token": "rules-token"}
    response = client.put(
        "/repositories/test/repo/rules",
        headers=headers,
        json={"rules": [{"rule_key": "python-naming", "description": "Use snake_case for Python functions.", "confidence": 0.9, "evidence_count": 5, "source_prs": [1, 2]}]},
    )
    assert response.status_code == 200
    rules = client.get("/repositories/test/repo/rules").json()["rules"]
    assert rules[0]["rule_key"] == "python-naming"
    assert rules[0]["evidence_count"] == 5


def test_github_delivery_event_is_audited(monkeypatch):
    monkeypatch.setenv("WORKER_STATUS_TOKEN", "delivery-token")
    job_id = client.post("/jobs", json=PAYLOAD).json()["job_id"]
    response = client.post(
        f"/jobs/{job_id}/delivery",
        headers={"X-Worker-Token": "delivery-token"},
        json={"status": "published", "metadata": {"html_url": "https://github.com/example/review/1"}},
    )
    assert response.status_code == 200
    assert client.get(f"/jobs/{job_id}/events").json()[-1]["event"] == "github_review_published"

@pytest.mark.parametrize(
    "mutation",
    [
        "summary_type",
        "agent_runs_type",
        "negative_tokens",
        "invalid_finding_location",
        "invalid_duration_type",
        "naive_timestamp",
    ],
)
def test_malformed_worker_result_is_rejected_without_completing_job(monkeypatch, mutation):
    monkeypatch.setenv("WORKER_STATUS_TOKEN", "result-token")
    job_id = client.post("/jobs", json=PAYLOAD).json()["job_id"]
    headers = {"X-Worker-Token": "result-token"}
    started = client.post(
        f"/jobs/{job_id}/status", json={"event": "started", "attempt": 1}, headers=headers
    )
    assert started.status_code == 200

    result = _valid_result(job_id)
    if mutation == "summary_type":
        result["summary"] = []
    elif mutation == "agent_runs_type":
        result["agent_runs"] = {}
    elif mutation == "negative_tokens":
        result["agent_runs"][0]["input_tokens"] = -1
    elif mutation == "invalid_finding_location":
        result["findings"][0]["line"] = 0
    elif mutation == "invalid_duration_type":
        result["duration_ms"] = "fast"
    elif mutation == "naive_timestamp":
        result["started_at"] = "2026-11-11T10:00:00"

    response = client.post(
        f"/jobs/{job_id}/status",
        json={"event": "completed", "result": result},
        headers=headers,
    )
    assert response.status_code == 422
    assert client.get(f"/jobs/{job_id}").json()["status"] == "RUNNING"
    assert client.get(f"/jobs/{job_id}/result").status_code == 404


def test_completed_callback_without_review_result_is_rejected(monkeypatch):
    monkeypatch.setenv("WORKER_STATUS_TOKEN", "result-token")
    job_id = client.post("/jobs", json=PAYLOAD).json()["job_id"]
    headers = {"X-Worker-Token": "result-token"}
    client.post(f"/jobs/{job_id}/status", json={"event": "started"}, headers=headers)
    response = client.post(f"/jobs/{job_id}/status", json={"event": "completed"}, headers=headers)
    assert response.status_code == 422
    assert client.get(f"/jobs/{job_id}").json()["status"] == "RUNNING"


def test_agent_checkpoint_round_trips_and_is_scoped_to_exact_context(monkeypatch):
    monkeypatch.setenv("WORKER_STATUS_TOKEN", "checkpoint-token")
    job_id = client.post("/jobs", json=PAYLOAD).json()["job_id"]
    headers = {"X-Worker-Token": "checkpoint-token"}
    fingerprint = "a" * 64
    checkpoint = {
        "findings": [],
        "agent_run": {
            "agent": "SECURITY", "duration_ms": 1234, "findings_count": 0,
            "rejected_findings_count": 0, "response_status": "empty",
            "input_tokens": 150, "output_tokens": 25, "model": "local-model",
            "source_context_files": 2, "input_truncated": False,
            "reused_checkpoint": False, "shared_call": False,
        },
    }
    denied = client.get(f"/jobs/{job_id}/checkpoints", params={"context_fingerprint": fingerprint})
    assert denied.status_code == 401
    saved = client.post(
        f"/jobs/{job_id}/checkpoints", headers=headers,
        json={"agent": "SECURITY", "context_fingerprint": fingerprint, "payload": checkpoint},
    )
    assert saved.status_code == 200
    restored = client.get(
        f"/jobs/{job_id}/checkpoints", headers=headers,
        params={"context_fingerprint": fingerprint},
    )
    assert restored.status_code == 200
    assert restored.json()["checkpoints"][0]["agent"] == "SECURITY"
    assert restored.json()["checkpoints"][0]["payload"] == checkpoint
    different_context = client.get(
        f"/jobs/{job_id}/checkpoints", headers=headers,
        params={"context_fingerprint": "b" * 64},
    )
    assert different_context.json()["checkpoints"] == []


def test_agent_checkpoint_rejects_mismatched_agent_run(monkeypatch):
    monkeypatch.setenv("WORKER_STATUS_TOKEN", "checkpoint-token")
    job_id = client.post("/jobs", json=PAYLOAD).json()["job_id"]
    response = client.post(
        f"/jobs/{job_id}/checkpoints",
        headers={"X-Worker-Token": "checkpoint-token"},
        json={
            "agent": "SECURITY", "context_fingerprint": "c" * 64,
            "payload": {"findings": [], "agent_run": {"agent": "ARCHITECTURE"}},
        },
    )
    assert response.status_code == 422
