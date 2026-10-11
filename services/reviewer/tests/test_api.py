import pytest
from app.main import app, get_reviewer
from app.reviewers import MockReviewer, MockScenario, Reviewer
from app.reviewers.base import Analysis
from app.schemas import ReviewerMode, ReviewResult
from fastapi.testclient import TestClient

from tests.conftest import SAMPLE_DIFF

PAYLOAD = {"job_id": "job-1", "repository": "test/repo", "pr_number": 7, "commit_sha": "abc123"}


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def use_reviewer(reviewer: Reviewer) -> None:
    app.dependency_overrides[get_reviewer] = lambda: reviewer


def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_info_reports_mock_reviewer(client):
    use_reviewer(MockReviewer())
    body = client.get("/info").json()
    assert body["mode"] == ReviewerMode.MOCK
    assert body["name"] == "fidelis-mock-reviewer"


def test_review_returns_contract_compliant_result(client):
    use_reviewer(MockReviewer())
    response = client.post("/review", json=PAYLOAD)

    assert response.status_code == 200
    result = ReviewResult.model_validate(response.json())  # full contract validation
    assert result.job_id == "job-1"
    assert result.summary.total_findings == 6
    assert result.findings[0].severity == "HIGH"


def test_review_uses_diff_when_supplied(client):
    use_reviewer(MockReviewer())
    body = client.post("/review", json={**PAYLOAD, "diff": SAMPLE_DIFF}).json()
    files = {f["file"] for f in body["findings"] if f["file"]}
    assert files <= {"app/users.py", "app/db.py"}


def test_clean_pr_returns_empty_findings(client):
    use_reviewer(MockReviewer(MockScenario.CLEAN))
    body = client.post("/review", json=PAYLOAD).json()
    assert body["findings"] == []
    assert body["summary"]["highest_severity"] is None


@pytest.mark.parametrize(
    ("scenario", "status", "code", "retryable"),
    [
        (MockScenario.FAIL_RETRYABLE, 502, "UPSTREAM_ERROR", True),
        (MockScenario.TIMEOUT, 504, "REVIEW_TIMEOUT", True),
        (MockScenario.FAIL_PERMANENT, 500, "INTERNAL_ERROR", False),
    ],
)
def test_reviewer_errors_map_to_error_response(client, scenario, status, code, retryable):
    use_reviewer(MockReviewer(scenario))
    response = client.post("/review", json=PAYLOAD)

    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert response.json()["error"]["retryable"] is retryable
    assert response.json()["error"]["message"]


def test_invalid_request_returns_422_error_response_not_fastapi_default(client):
    use_reviewer(MockReviewer())
    response = client.post("/review", json={**PAYLOAD, "pr_number": 0, "repository": "bad"})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "INVALID_REQUEST" and error["retryable"] is False
    locs = {tuple(e["loc"]) for e in error["details"]["errors"]}
    assert ("body", "pr_number") in locs and ("body", "repository") in locs
    assert "detail" not in response.json()


def test_unknown_field_is_rejected(client):
    use_reviewer(MockReviewer())
    response = client.post("/review", json={**PAYLOAD, "surprise": True})
    assert response.status_code == 422


def test_non_json_body_is_a_422_error_response(client):
    use_reviewer(MockReviewer())
    response = client.post("/review", content=b"not json", headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


class _ExplodingReviewer(Reviewer):
    name, version, mode = "boom", "0", ReviewerMode.MOCK

    async def analyze(self, request) -> Analysis:
        raise RuntimeError("secret internal detail /etc/passwd")


def test_unexpected_exception_becomes_generic_500_without_leaking():
    app.dependency_overrides[get_reviewer] = lambda: _ExplodingReviewer()
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            response = c.post("/review", json=PAYLOAD)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert response.json()["error"]["retryable"] is False
    assert "secret" not in response.text and "passwd" not in response.text


def test_openapi_documents_error_responses(client):
    schema = client.get("/openapi.json").json()
    responses = schema["paths"]["/review"]["post"]["responses"]
    assert {"200", "422", "500", "502", "503", "504"} <= set(responses)
    assert "ErrorResponse" in schema["components"]["schemas"]
    assert "ReviewResult" in schema["components"]["schemas"]


def test_default_reviewer_comes_from_environment(monkeypatch):
    monkeypatch.setenv("REVIEWER_MOCK_SCENARIO", "clean")
    get_reviewer.cache_clear()
    try:
        with TestClient(app) as c:
            assert c.post("/review", json=PAYLOAD).json()["findings"] == []
    finally:
        get_reviewer.cache_clear()
