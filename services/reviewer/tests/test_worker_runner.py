import pytest
from app.reviewers import MockReviewer, MockScenario
from app.schemas import (
    ErrorCode,
    ErrorDetail,
    ReviewerError,
    ReviewRequest,
    ReviewResult,
)
from app.worker import (
    ExecutionDisposition,
    ExecutionOutcome,
    InMemoryStatusReporter,
    InProcessReviewerClient,
    InvalidTransitionError,
    JobStatus,
    ReviewerClient,
    ReviewWorker,
    backoff_seconds,
)

from tests.conftest import FakeClock


class ScriptedClient(ReviewerClient):
    """Plays back a list of results/exceptions, one per call."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls: list[ReviewRequest] = []

    def review(self, request):
        self.calls.append(request)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def reporter(request_obj) -> InMemoryStatusReporter:
    r = InMemoryStatusReporter(clock=FakeClock(step_ms=100))
    r.add_job(request_obj.job_id)
    return r


def good_result(request: ReviewRequest) -> ReviewResult:
    import asyncio

    return asyncio.run(MockReviewer().review(request))


def retryable(msg="upstream down") -> ReviewerError:
    return ReviewerError(ErrorCode.UPSTREAM_ERROR, msg)


def permanent(msg="nope") -> ReviewerError:
    return ReviewerError(ErrorCode.INTERNAL_ERROR, msg)


def make_worker(client, reporter, **kw) -> ReviewWorker:
    return ReviewWorker(client, reporter, clock=FakeClock(step_ms=50), **kw)


def path(reporter) -> list[str]:
    return [reporter.history[0].from_status.value, *(c.to_status.value for c in reporter.history)]


# ------------------------------- success ---------------------------------- #
def test_success_reports_running_then_completed(request_obj, reporter):
    result = good_result(request_obj)
    outcome = make_worker(ScriptedClient(result), reporter).execute(request_obj)

    assert outcome.disposition is ExecutionDisposition.SUCCEEDED
    assert outcome.job_status is JobStatus.COMPLETED
    assert outcome.result == result and outcome.error is None
    assert (outcome.attempt, outcome.max_attempts) == (1, 3)
    assert outcome.duration_ms == 50
    assert path(reporter) == ["QUEUED", "RUNNING", "COMPLETED"]

    job = reporter.jobs[request_obj.job_id]
    assert job.status is JobStatus.COMPLETED
    assert job.result == result
    assert job.started_at is not None and job.completed_at > job.started_at
    assert job.error_message is None and job.attempts == 1


def test_client_receives_the_request_unchanged(request_obj, reporter):
    client = ScriptedClient(good_result(request_obj))
    make_worker(client, reporter).execute(request_obj)
    assert client.calls == [request_obj]


# ------------------------------- retries ---------------------------------- #
def test_retryable_error_requeues_the_job_with_backoff(request_obj, reporter):
    client = ScriptedClient(retryable())
    outcome = make_worker(client, reporter).execute(request_obj, attempt=1)

    assert outcome.disposition is ExecutionDisposition.RETRY
    assert outcome.job_status is JobStatus.QUEUED
    assert outcome.retry_after_seconds == 2.0
    assert outcome.error.code is ErrorCode.UPSTREAM_ERROR and outcome.error.retryable
    assert outcome.result is None
    assert path(reporter) == ["QUEUED", "RUNNING", "QUEUED"]
    job = reporter.jobs[request_obj.job_id]
    assert job.error_message == "upstream down" and job.completed_at is None
    assert reporter.history[-1].retry_after_seconds == 2.0


def test_backoff_grows_with_each_attempt(request_obj, reporter):
    worker = make_worker(ScriptedClient(retryable(), retryable()), reporter)
    first = worker.execute(request_obj, attempt=1)
    second = worker.execute(request_obj, attempt=2)
    assert (first.retry_after_seconds, second.retry_after_seconds) == (2.0, 4.0)


def test_retry_then_success_clears_error_and_keeps_first_started_at(request_obj, reporter):
    worker = make_worker(ScriptedClient(retryable(), good_result(request_obj)), reporter)
    worker.execute(request_obj, attempt=1)
    first_started = reporter.jobs[request_obj.job_id].started_at
    outcome = worker.execute(request_obj, attempt=2)

    assert outcome.disposition is ExecutionDisposition.SUCCEEDED
    job = reporter.jobs[request_obj.job_id]
    assert job.status is JobStatus.COMPLETED and job.error_message is None
    assert job.started_at == first_started
    assert job.attempts == 2
    assert path(reporter) == ["QUEUED", "RUNNING", "QUEUED", "RUNNING", "COMPLETED"]


def test_retryable_error_on_last_attempt_fails_the_job(request_obj, reporter):
    worker = make_worker(ScriptedClient(retryable("still down")), reporter, max_attempts=3)
    outcome = worker.execute(request_obj, attempt=3)

    assert outcome.disposition is ExecutionDisposition.FAILED
    assert outcome.job_status is JobStatus.FAILED
    assert outcome.retry_after_seconds is None
    job = reporter.jobs[request_obj.job_id]
    assert job.status is JobStatus.FAILED and job.error_message == "still down"
    assert job.completed_at is not None


def test_non_retryable_error_fails_immediately_even_with_attempts_left(request_obj, reporter):
    outcome = make_worker(ScriptedClient(permanent()), reporter, max_attempts=5).execute(request_obj)
    assert outcome.disposition is ExecutionDisposition.FAILED
    assert path(reporter) == ["QUEUED", "RUNNING", "FAILED"]


def test_max_attempts_of_one_never_retries(request_obj, reporter):
    outcome = make_worker(ScriptedClient(retryable()), reporter, max_attempts=1).execute(request_obj)
    assert outcome.disposition is ExecutionDisposition.FAILED


# --------------------------- unexpected / invalid -------------------------- #
def test_unexpected_exception_fails_the_job_as_internal_error(request_obj, reporter):
    outcome = make_worker(ScriptedClient(KeyError("boom")), reporter).execute(request_obj)

    assert outcome.disposition is ExecutionDisposition.FAILED
    assert outcome.error.code is ErrorCode.INTERNAL_ERROR and outcome.error.retryable is False
    assert "KeyError" in outcome.error.message
    assert reporter.jobs[request_obj.job_id].status is JobStatus.FAILED


def test_unexpected_exception_message_is_truncated(request_obj, reporter):
    outcome = make_worker(ScriptedClient(RuntimeError("x" * 5000)), reporter).execute(request_obj)
    assert len(outcome.error.message) == 500


@pytest.mark.parametrize("field", ["job_id", "commit_sha", "pr_number", "repository"])
def test_result_for_a_different_job_is_rejected(request_obj, reporter, field):
    other = {
        "job_id": "other-job",
        "commit_sha": "ffff",
        "pr_number": 999,
        "repository": "someone/else",
    }
    foreign = good_result(request_obj.model_copy(update={field: other[field]}))
    outcome = make_worker(ScriptedClient(foreign), reporter).execute(request_obj)

    assert outcome.disposition is ExecutionDisposition.FAILED
    assert outcome.error.code is ErrorCode.INVALID_RESPONSE
    assert field in outcome.error.message
    assert reporter.jobs[request_obj.job_id].result is None


# ----------------------- infrastructure failures propagate ----------------- #
def test_attempt_must_be_within_bounds(request_obj, reporter):
    worker = make_worker(ScriptedClient(), reporter, max_attempts=3)
    for bad in (0, 4, -1):
        with pytest.raises(ValueError, match="attempt"):
            worker.execute(request_obj, attempt=bad)
    assert reporter.history == []  # nothing was reported


def test_reporter_failure_on_start_propagates_and_reviewer_is_not_called(request_obj):
    reporter = InMemoryStatusReporter()  # job never registered -> UnknownJobError
    client = ScriptedClient()
    from app.worker import UnknownJobError

    with pytest.raises(UnknownJobError):
        make_worker(client, reporter).execute(request_obj)
    assert client.calls == []


def test_reporter_failure_on_completion_propagates(request_obj, reporter, monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("db down")

    monkeypatch.setattr(reporter, "job_completed", boom)
    with pytest.raises(ConnectionError):
        make_worker(ScriptedClient(good_result(request_obj)), reporter).execute(request_obj)


# ------------------------- crash recovery / duplicates --------------------- #
def test_redelivery_after_worker_crash_resumes_a_running_job(request_obj, reporter):
    reporter.job_started(request_obj.job_id, attempt=1)  # previous worker died here
    outcome = make_worker(ScriptedClient(good_result(request_obj)), reporter).execute(request_obj)

    assert outcome.disposition is ExecutionDisposition.SUCCEEDED
    assert path(reporter) == ["QUEUED", "RUNNING", "RUNNING", "COMPLETED"]


def test_duplicate_delivery_for_a_finished_job_is_rejected_before_reviewing(request_obj, reporter):
    client = ScriptedClient(good_result(request_obj), good_result(request_obj))
    worker = make_worker(client, reporter)
    worker.execute(request_obj)
    with pytest.raises(InvalidTransitionError) as excinfo:
        worker.execute(request_obj)
    assert (excinfo.value.current, excinfo.value.target) == (JobStatus.COMPLETED, JobStatus.RUNNING)
    assert len(client.calls) == 1  # the duplicate never reached the reviewer


# ------------------------------ run_to_completion -------------------------- #
def test_run_to_completion_flaky_succeeds_on_third_attempt(request_obj, reporter):
    sleeps: list[float] = []
    client = InProcessReviewerClient(MockReviewer(MockScenario.FLAKY, flaky_failures=2))
    outcomes = make_worker(client, reporter).run_to_completion(request_obj, sleep=sleeps.append)

    assert [o.disposition.value for o in outcomes] == ["RETRY", "RETRY", "SUCCEEDED"]
    assert sleeps == [2.0, 4.0]
    assert reporter.jobs[request_obj.job_id].status is JobStatus.COMPLETED


def test_run_to_completion_gives_up_after_max_attempts(request_obj, reporter):
    client = InProcessReviewerClient(MockReviewer(MockScenario.TIMEOUT))
    outcomes = make_worker(client, reporter, max_attempts=3).run_to_completion(
        request_obj, sleep=lambda _: None
    )
    assert [o.disposition.value for o in outcomes] == ["RETRY", "RETRY", "FAILED"]
    assert reporter.jobs[request_obj.job_id].status is JobStatus.FAILED


def test_run_to_completion_stops_on_permanent_failure(request_obj, reporter):
    client = InProcessReviewerClient(MockReviewer(MockScenario.FAIL_PERMANENT))
    outcomes = make_worker(client, reporter).run_to_completion(request_obj, sleep=lambda _: None)
    assert len(outcomes) == 1 and outcomes[0].disposition is ExecutionDisposition.FAILED


# --------------------------------- backoff --------------------------------- #
@pytest.mark.parametrize(
    ("attempt", "expected"), [(1, 2.0), (2, 4.0), (3, 8.0), (5, 32.0), (6, 60.0), (20, 60.0)]
)
def test_backoff_is_exponential_and_capped(attempt, expected):
    assert backoff_seconds(attempt) == expected


def test_backoff_custom_values_and_validation():
    assert backoff_seconds(3, base=1.5, cap=100) == 6.0
    with pytest.raises(ValueError):
        backoff_seconds(0)


def test_worker_uses_configured_backoff(request_obj, reporter):
    worker = make_worker(
        ScriptedClient(retryable()), reporter, backoff_base_seconds=0.5, backoff_cap_seconds=1
    )
    assert worker.execute(request_obj).retry_after_seconds == 0.5


def test_worker_constructor_validation(reporter):
    with pytest.raises(ValueError, match="max_attempts"):
        ReviewWorker(ScriptedClient(), reporter, max_attempts=0)
    with pytest.raises(ValueError, match="backoff"):
        ReviewWorker(ScriptedClient(), reporter, backoff_base_seconds=-1)


# ---------------------------- ExecutionOutcome ----------------------------- #
def test_outcome_is_json_serialisable_for_celery(request_obj, reporter):
    outcome = make_worker(ScriptedClient(good_result(request_obj)), reporter).execute(request_obj)
    payload = outcome.model_dump(mode="json")
    assert ExecutionOutcome.model_validate(payload) == outcome


def _err() -> ErrorDetail:
    return ErrorDetail(code=ErrorCode.UPSTREAM_ERROR, message="m", retryable=True)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"disposition": "SUCCEEDED", "job_status": "COMPLETED"},  # no result
        {"disposition": "RETRY", "job_status": "QUEUED", "error": _err()},  # no delay
        {"disposition": "RETRY", "job_status": "RUNNING", "error": _err(), "retry_after_seconds": 1},
        {"disposition": "FAILED", "job_status": "FAILED"},  # no error
        {"disposition": "FAILED", "job_status": "FAILED", "error": _err(), "retry_after_seconds": 1},
        {"disposition": "FAILED", "job_status": "FAILED", "error": _err(), "attempt": 4},
    ],
)
def test_outcome_rejects_inconsistent_fields(kwargs):
    base = {"job_id": "j", "attempt": 1, "max_attempts": 3, "duration_ms": 1}
    with pytest.raises(ValueError):
        ExecutionOutcome(**{**base, **kwargs})
