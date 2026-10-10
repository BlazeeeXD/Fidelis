import pytest
from app.db import models  # noqa: F401
from app.db.database import Base
from app.main import app
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

    yield

    engine.dispose()


client = TestClient(app)


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
