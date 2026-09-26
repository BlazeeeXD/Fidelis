from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


def test_create_job():
    response = client.post(
        "/jobs",
        json={"repository": "test/repo", "pr_number": 1, "commit_sha": "abc123"},
    )
    assert response.status_code == 201
    body = response.json()
    assert "job_id" in body
    assert body["status"] == "QUEUED"


def test_retrieve_job():
    create = client.post(
        "/jobs",
        json={"repository": "test/repo", "pr_number": 2, "commit_sha": "def456"},
    )
    job_id = create.json()["job_id"]

    response = client.get(f"/jobs/{job_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["job_id"] == job_id
    assert body["repository"] == "test/repo"
    assert body["pr_number"] == 2
    assert body["status"] == "QUEUED"


def test_retrieve_nonexistent_job():
    response = client.get("/jobs/random-nonexistent-id")
    assert response.status_code == 404