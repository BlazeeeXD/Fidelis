import asyncio
import json

import httpx
import pytest
from app.main import app, get_reviewer
from app.reviewers import MockReviewer, MockScenario
from app.schemas import ErrorCode, ReviewerError, ReviewRequest, ReviewResult
from app.worker import (
    ExecutionDisposition,
    HttpReviewerClient,
    InMemoryStatusReporter,
    InProcessReviewerClient,
    JobStatus,
    ReviewWorker,
)
from fastapi.testclient import TestClient

from tests.conftest import SAMPLE_DIFF


def http_client_for(handler) -> HttpReviewerClient:
    transport = httpx.MockTransport(handler)
    return HttpReviewerClient(client=httpx.Client(base_url="http://reviewer:8000", transport=transport))


# ------------------------------- in-process -------------------------------- #
def test_in_process_client_returns_a_validated_result(request_obj):
    result = InProcessReviewerClient(MockReviewer()).review(request_obj)
    assert isinstance(result, ReviewResult) and result.job_id == request_obj.job_id


def test_in_process_client_propagates_reviewer_errors(request_obj):
    with pytest.raises(ReviewerError) as excinfo:
        InProcessReviewerClient(MockReviewer(MockScenario.TIMEOUT)).review(request_obj)
    assert excinfo.value.code is ErrorCode.REVIEW_TIMEOUT


def test_in_process_client_refuses_to_run_inside_an_event_loop(request_obj):
    client = InProcessReviewerClient(MockReviewer())

    async def inside_loop():
        client.review(request_obj)

    with pytest.raises(RuntimeError, match="synchronous"):
        asyncio.run(inside_loop())


# ---------------------------------- http ----------------------------------- #
@pytest.fixture
def real_app_client():
    """HttpReviewerClient talking to the real FastAPI app (in-process transport)."""

    def make(reviewer) -> HttpReviewerClient:
        app.dependency_overrides[get_reviewer] = lambda: reviewer
        return HttpReviewerClient(client=TestClient(app, base_url="http://reviewer:8000"))

    yield make
    app.dependency_overrides.clear()


def test_http_client_success_against_the_real_app(real_app_client, request_obj):
    result = real_app_client(MockReviewer()).review(request_obj)
    assert result.summary.total_findings == 6


@pytest.mark.parametrize(
    ("scenario", "code", "retryable"),
    [
        (MockScenario.FAIL_RETRYABLE, ErrorCode.UPSTREAM_ERROR, True),
        (MockScenario.TIMEOUT, ErrorCode.REVIEW_TIMEOUT, True),
        (MockScenario.FAIL_PERMANENT, ErrorCode.INTERNAL_ERROR, False),
    ],
)
def test_http_client_maps_error_responses(real_app_client, request_obj, scenario, code, retryable):
    with pytest.raises(ReviewerError) as excinfo:
        real_app_client(MockReviewer(scenario)).review(request_obj)
    assert (excinfo.value.code, excinfo.value.retryable) == (code, retryable)


def test_http_client_surfaces_validation_errors_as_non_retryable(real_app_client):
    # A request the client-side model would never build: bypass it with a raw call.
    client = real_app_client(MockReviewer())
    bad = ReviewRequest.model_construct(job_id="j", repository="bad", pr_number=0, commit_sha="a",
                                        schema_version="1.0", diff=None)
    with pytest.raises(ReviewerError) as excinfo:
        client.review(bad)
    assert excinfo.value.code is ErrorCode.INVALID_REQUEST and not excinfo.value.retryable
    assert excinfo.value.details["errors"]


def test_http_client_sends_diff_only_when_present(request_obj):
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(500, json={"error": {"code": "INTERNAL_ERROR", "message": "x", "retryable": False}})

    client = http_client_for(handler)
    for req in (request_obj, request_obj.model_copy(update={"diff": SAMPLE_DIFF})):
        with pytest.raises(ReviewerError):
            client.review(req)
    assert "diff" not in sent[0] and sent[1]["diff"] == SAMPLE_DIFF
    assert sent[0]["job_id"] == request_obj.job_id


@pytest.mark.parametrize(
    "exc",
    [httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), httpx.ConnectTimeout("slow")],
)
def test_transport_failures_are_retryable_unavailable(request_obj, exc):
    def handler(request):
        raise exc

    with pytest.raises(ReviewerError) as excinfo:
        http_client_for(handler).review(request_obj)
    assert excinfo.value.code is ErrorCode.REVIEWER_UNAVAILABLE and excinfo.value.retryable


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (502, ErrorCode.REVIEWER_UNAVAILABLE, True),
        (503, ErrorCode.REVIEWER_UNAVAILABLE, True),
        (504, ErrorCode.REVIEWER_UNAVAILABLE, True),
        (500, ErrorCode.REVIEWER_UNAVAILABLE, True),
        (429, ErrorCode.REVIEWER_UNAVAILABLE, True),
        (408, ErrorCode.REVIEWER_UNAVAILABLE, True),
        (404, ErrorCode.INVALID_REQUEST, False),
        (400, ErrorCode.INVALID_REQUEST, False),
    ],
)
def test_unparseable_error_bodies_are_classified_by_status(request_obj, status, code, retryable):
    client = http_client_for(lambda r: httpx.Response(status, text="<html>proxy error</html>"))
    with pytest.raises(ReviewerError) as excinfo:
        client.review(request_obj)
    assert (excinfo.value.code, excinfo.value.retryable) == (code, retryable)


@pytest.mark.parametrize(
    "body",
    [b"not json", b"{}", b'{"job_id": "j"}', b"[]"],
)
def test_malformed_success_body_is_invalid_response(request_obj, body):
    client = http_client_for(lambda r: httpx.Response(200, content=body))
    with pytest.raises(ReviewerError) as excinfo:
        client.review(request_obj)
    assert excinfo.value.code is ErrorCode.INVALID_RESPONSE and excinfo.value.retryable is False


def test_success_body_with_inconsistent_summary_is_invalid_response(request_obj):
    payload = InProcessReviewerClient(MockReviewer()).review(request_obj).model_dump(mode="json")
    payload["summary"]["total_findings"] = 0
    client = http_client_for(lambda r: httpx.Response(200, json=payload))
    with pytest.raises(ReviewerError) as excinfo:
        client.review(request_obj)
    assert excinfo.value.code is ErrorCode.INVALID_RESPONSE


def test_context_manager_closes_only_an_owned_client():
    with HttpReviewerClient("http://reviewer:8000") as owned:
        inner = owned._client
        assert not inner.is_closed
    assert inner.is_closed

    injected = httpx.Client(base_url="http://x")
    with HttpReviewerClient(client=injected):
        pass
    assert not injected.is_closed
    injected.close()


# --------------- worker + HTTP client + real app, end to end --------------- #
def test_worker_over_http_retries_a_flaky_reviewer_to_completion(real_app_client, request_obj):
    reviewer = MockReviewer(MockScenario.FLAKY, flaky_failures=2)  # one instance: counters persist
    reporter = InMemoryStatusReporter()
    reporter.add_job(request_obj.job_id)
    worker = ReviewWorker(real_app_client(reviewer), reporter, max_attempts=3)

    outcomes = worker.run_to_completion(request_obj, sleep=lambda _: None)

    assert [o.disposition for o in outcomes] == [
        ExecutionDisposition.RETRY, ExecutionDisposition.RETRY, ExecutionDisposition.SUCCEEDED,
    ]
    assert outcomes[0].error.code is ErrorCode.UPSTREAM_ERROR  # error came back through HTTP intact
    assert reporter.status_of(request_obj.job_id) is JobStatus.COMPLETED
    assert reporter.jobs[request_obj.job_id].result.summary.total_findings == 6
