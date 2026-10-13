"""Mock review execution — the whole worker lifecycle with no infrastructure.

``QUEUED -> RUNNING -> (retry?) -> COMPLETED | FAILED`` using the mock reviewer
and an in-memory status reporter. It shows P3/P5 exactly what a real run looks
like and gives them a one-liner to reproduce each scenario.

Run it::

    python -m app.worker.demo                      # happy path
    python -m app.worker.demo --scenario flaky     # fails twice, then succeeds
    python -m app.worker.demo --scenario fail_permanent
"""

from dataclasses import dataclass

from app.reviewers import MockReviewer, MockScenario
from app.schemas import ReviewRequest
from app.worker.clients import InProcessReviewerClient
from app.worker.interface import ExecutionOutcome, JobStatus
from app.worker.reporters import InMemoryStatusReporter, JobRecord, StatusChange
from app.worker.runner import ReviewWorker


@dataclass(frozen=True)
class MockExecutionReport:
    """Everything that happened during one mock execution."""

    request: ReviewRequest
    outcomes: list[ExecutionOutcome]
    history: list[StatusChange]
    job: JobRecord

    @property
    def final_status(self) -> JobStatus:
        return self.job.status

    @property
    def transitions(self) -> list[str]:
        """Human-readable path, e.g. ``['QUEUED', 'RUNNING', 'QUEUED', 'RUNNING', 'COMPLETED']``."""
        if not self.history:
            return [JobStatus.QUEUED.value]
        return [self.history[0].from_status.value, *(c.to_status.value for c in self.history)]


def run_mock_execution(
    scenario: MockScenario = MockScenario.MULTI_AGENT,
    *,
    max_attempts: int = 3,
    flaky_failures: int = 2,
    request: ReviewRequest | None = None,
) -> MockExecutionReport:
    """Create a job, run it to a terminal state with the mock reviewer, report back.

    Backoff delays are skipped (nothing actually sleeps).
    """
    request = request or ReviewRequest(
        job_id="mock-job-1", repository="acme/payments-api", pr_number=42, commit_sha="abc123"
    )
    reporter = InMemoryStatusReporter()
    reporter.add_job(request.job_id)
    reviewer = MockReviewer(scenario, flaky_failures=flaky_failures)
    worker = ReviewWorker(InProcessReviewerClient(reviewer), reporter, max_attempts=max_attempts)

    outcomes = worker.run_to_completion(request, sleep=lambda _seconds: None)
    return MockExecutionReport(
        request=request,
        outcomes=outcomes,
        history=list(reporter.history),
        job=reporter.jobs[request.job_id],
    )
