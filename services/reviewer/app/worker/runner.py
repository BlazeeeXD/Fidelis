"""The review worker: execute one job attempt and keep job state truthful.

This is the logic a Celery task wraps. It deliberately knows nothing about
Celery, Redis or SQL — those are plugged in through
:class:`~app.worker.interface.ReviewerClient` and
:class:`~app.worker.interface.JobStatusReporter`.

One call to :meth:`ReviewWorker.execute` is one *attempt*:

1. report ``RUNNING``;
2. ask the reviewer for the review;
3. depending on how that went, report exactly one of
   ``COMPLETED`` / back to ``QUEUED`` (retry) / ``FAILED``, and return an
   :class:`ExecutionOutcome` saying what the caller should do next.

Reviewer failures never raise out of ``execute`` — they become an outcome.
Exceptions that *do* propagate are infrastructure failures of the reporter
(e.g. database down); let the task runner's own retry deal with those.
"""

import logging
import time
from collections.abc import Callable

from app.reviewers.base import Clock, utcnow
from app.schemas import (
    ErrorCode,
    ErrorDetail,
    ReviewerError,
    ReviewRequest,
    ReviewResult,
)
from app.worker.interface import (
    ExecutionDisposition,
    ExecutionOutcome,
    JobStatus,
    JobStatusReporter,
    ReviewerClient,
)

logger = logging.getLogger("reviewer.worker")

_MAX_ERROR_MESSAGE = 500


def backoff_seconds(attempt: int, *, base: float = 2.0, cap: float = 60.0) -> float:
    """Exponential backoff after failed ``attempt`` (1-based): base, 2*base, 4*base, ... <= cap."""
    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    return float(min(cap, base * 2 ** (attempt - 1)))


class ReviewWorker:
    """Runs review attempts for jobs, reporting status as it goes."""

    def __init__(
        self,
        client: ReviewerClient,
        reporter: JobStatusReporter,
        *,
        max_attempts: int = 3,
        backoff_base_seconds: float = 2.0,
        backoff_cap_seconds: float = 60.0,
        clock: Clock = utcnow,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if backoff_base_seconds < 0 or backoff_cap_seconds < 0:
            raise ValueError("backoff values must be >= 0")
        self._client = client
        self._reporter = reporter
        self.max_attempts = max_attempts
        self._backoff_base = backoff_base_seconds
        self._backoff_cap = backoff_cap_seconds
        self._clock = clock

    # ------------------------------------------------------------------ #
    def execute(self, request: ReviewRequest, *, attempt: int = 1) -> ExecutionOutcome:
        """Run attempt number ``attempt`` (1-based) of the job in ``request``."""
        if not 1 <= attempt <= self.max_attempts:
            raise ValueError(f"attempt must be between 1 and {self.max_attempts}, got {attempt}")

        job_id = request.job_id
        started = self._clock()
        self._reporter.job_started(job_id, attempt=attempt)

        try:
            result = self._review_and_verify(request)
        except ReviewerError as exc:
            return self._handle_error(request, exc, attempt, started)
        except Exception as exc:  # a bug in the client/reviewer, not a controlled failure
            logger.exception("job %s: unexpected error on attempt %s", job_id, attempt)
            detail = f"Unexpected error while running review: {type(exc).__name__}: {exc}"
            error = ReviewerError(ErrorCode.INTERNAL_ERROR, detail[:_MAX_ERROR_MESSAGE])
            return self._handle_error(request, error, attempt, started)

        self._reporter.job_completed(job_id, result)
        return ExecutionOutcome(
            job_id=job_id,
            disposition=ExecutionDisposition.SUCCEEDED,
            job_status=JobStatus.COMPLETED,
            attempt=attempt,
            max_attempts=self.max_attempts,
            result=result,
            duration_ms=self._elapsed_ms(started),
        )

    def run_to_completion(
        self,
        request: ReviewRequest,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> list[ExecutionOutcome]:
        """Run attempts until the job succeeds or fails; return every outcome.

        This is what Celery's retry machinery does for you in production. It
        exists so tests and the mock execution can exercise the whole lifecycle
        in one call. Pass ``sleep=lambda _: None`` to skip the backoff delays.
        """
        outcomes: list[ExecutionOutcome] = []
        for attempt in range(1, self.max_attempts + 1):
            outcome = self.execute(request, attempt=attempt)
            outcomes.append(outcome)
            if outcome.disposition is not ExecutionDisposition.RETRY:
                break
            sleep(outcome.retry_after_seconds or 0.0)
        return outcomes

    # ------------------------------------------------------------------ #
    def _review_and_verify(self, request: ReviewRequest) -> ReviewResult:
        result = self._client.review(request)
        mismatched = [
            name
            for name in ("job_id", "repository", "pr_number", "commit_sha")
            if getattr(result, name) != getattr(request, name)
        ]
        if mismatched:
            raise ReviewerError(
                ErrorCode.INVALID_RESPONSE,
                f"Reviewer returned a result for a different job ({', '.join(mismatched)} differ).",
            )
        return result

    def _handle_error(
        self, request: ReviewRequest, exc: ReviewerError, attempt: int, started
    ) -> ExecutionOutcome:
        job_id = request.job_id
        error: ErrorDetail = exc.to_detail()
        duration_ms = self._elapsed_ms(started)

        if exc.retryable and attempt < self.max_attempts:
            delay = backoff_seconds(attempt, base=self._backoff_base, cap=self._backoff_cap)
            self._reporter.job_retry_scheduled(
                job_id, attempt=attempt, error=error, retry_after_seconds=delay
            )
            return ExecutionOutcome(
                job_id=job_id,
                disposition=ExecutionDisposition.RETRY,
                job_status=JobStatus.QUEUED,
                attempt=attempt,
                max_attempts=self.max_attempts,
                error=error,
                retry_after_seconds=delay,
                duration_ms=duration_ms,
            )

        self._reporter.job_failed(job_id, attempt=attempt, error=error)
        return ExecutionOutcome(
            job_id=job_id,
            disposition=ExecutionDisposition.FAILED,
            job_status=JobStatus.FAILED,
            attempt=attempt,
            max_attempts=self.max_attempts,
            error=error,
            duration_ms=duration_ms,
        )

    def _elapsed_ms(self, started) -> int:
        return max(0, round((self._clock() - started).total_seconds() * 1000))
