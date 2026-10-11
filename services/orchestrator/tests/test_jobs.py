import pytest
from app.db import models  # noqa: F401
from app.db.database import Base
from app.db.models import ReviewJobDB
from app.main import app
from app.models.job import JobStatus
from app.services.orchestrator import orchestrator_service
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.fixture(autouse=True)
def configure_test_database(monkeypatch):
    """Give every test its own in-memory SQLite database."""
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