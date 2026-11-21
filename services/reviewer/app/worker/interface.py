"""Interfaces between a review *worker* and the rest of the system.

A worker (Celery task, in P3's design) does one thing: take a queued review job,
get it reviewed, and make sure the job's state in the orchestrator ends up
truthful. Two seams make that testable without Redis, Celery, a database or a
network:

* :class:`ReviewerClient`   — "go and get this PR reviewed" (HTTP in production,
  in-process for tests).
* :class:`JobStatusReporter` — "record what happened to this job" (a database
  write via the orchestrator in production, in-memory for tests).

:class:`~app.worker.runner.ReviewWorker` wires the two together and owns the
status state machine and the retry decision.

Job statuses are the fixed set from ``docs/contracts/review-job-contract.md`` —
do not extend them without team agreement::

    QUEUED ──▶ RUNNING ──▶ COMPLETED
       ▲          │
       └─(retry)──┤
                  └──────▶ FAILED
"""

from abc import ABC, abstractmethod
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas import ErrorDetail, ReviewRequest, ReviewResult


class JobStatus(StrEnum):
    """Mirror of the orchestrator's ``JobStatus`` (kept in sync by a test)."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


#: Allowed status transitions. COMPLETED and FAILED are terminal.
ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset({JobStatus.RUNNING}),
    # RUNNING -> RUNNING is the crash-recovery re-entry: a redelivered task
    # starts a job whose previous worker died while it was RUNNING.
    JobStatus.RUNNING: frozenset(
        {JobStatus.RUNNING, JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.QUEUED}
    ),
    JobStatus.COMPLETED: frozenset(),
    JobStatus.FAILED: frozenset(),
}


class InvalidTransitionError(Exception):
    """A reporter was asked to move a job to a status it cannot reach."""

    def __init__(self, job_id: str, current: JobStatus, target: JobStatus) -> None:
        super().__init__(f"job {job_id}: illegal transition {current.value} -> {target.value}")
        self.job_id = job_id
        self.current = current
        self.target = target


class UnknownJobError(Exception):
    """A reporter was asked about a job id it has never seen."""


class ExecutionDisposition(StrEnum):
    """What the worker tells its caller to do next."""

    SUCCEEDED = "SUCCEEDED"  # job is COMPLETED; nothing more to do
    RETRY = "RETRY"  # job was put back to QUEUED; run again after the delay
    FAILED = "FAILED"  # job is FAILED; do not retry


class ExecutionOutcome(BaseModel):
    """Result of one execution attempt (JSON-serialisable for Celery results)."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    disposition: ExecutionDisposition
    job_status: JobStatus = Field(description="Status the job was left in.")
    attempt: int = Field(ge=1)
    max_attempts: int = Field(ge=1)
    result: ReviewResult | None = None
    error: ErrorDetail | None = None
    retry_after_seconds: float | None = Field(
        default=None, ge=0, description="Only set when disposition is RETRY."
    )
    duration_ms: int = Field(ge=0, description="Wall time of this attempt.")

    @model_validator(mode="after")
    def _check_shape(self) -> "ExecutionOutcome":
        d = self.disposition
        if d is ExecutionDisposition.SUCCEEDED:
            ok = (
                self.result is not None
                and self.error is None
                and self.retry_after_seconds is None
                and self.job_status is JobStatus.COMPLETED
            )
        elif d is ExecutionDisposition.RETRY:
            ok = (
                self.result is None
                and self.error is not None
                and self.retry_after_seconds is not None
                and self.job_status is JobStatus.QUEUED
            )
        else:  # FAILED
            ok = (
                self.result is None
                and self.error is not None
                and self.retry_after_seconds is None
                and self.job_status is JobStatus.FAILED
            )
        if not ok:
            raise ValueError(f"fields are inconsistent with disposition {d.value}")
        if self.attempt > self.max_attempts:
            raise ValueError("attempt must not exceed max_attempts")
        return self


class ReviewerClient(ABC):
    """Synchronous call into a reviewer. Raises ``ReviewerError`` on failure.

    Synchronous on purpose: Celery tasks are plain sync functions. Whatever the
    transport, every failure must surface as a :class:`~app.schemas.ReviewerError`
    carrying a code and a ``retryable`` flag — never a raw ``httpx`` exception.
    """

    @abstractmethod
    def review(self, request: ReviewRequest) -> ReviewResult:
        """Return the review for ``request`` or raise ``ReviewerError``."""


class JobStatusReporter(ABC):
    """Records job lifecycle events (the orchestrator/database side, P3 + P4).

    Contract for implementers:

    * Each method performs exactly the status transition named below and must
      raise :class:`InvalidTransitionError` if the job's current status does not
      allow it (see :data:`ALLOWED_TRANSITIONS`).
    * Methods must be safe to call from a worker process (own DB session, commit
      before returning). If they raise, the exception propagates to the task
      runner — the worker never swallows reporter failures.
    * ``started_at`` is set on the first start and kept across retries;
      ``completed_at`` is set on COMPLETED and FAILED.
    """

    @abstractmethod
    def job_started(self, job_id: str, *, attempt: int) -> None:
        """QUEUED -> RUNNING (or RUNNING -> RUNNING on crash recovery)."""

    @abstractmethod
    def job_completed(self, job_id: str, result: ReviewResult) -> None:
        """RUNNING -> COMPLETED; persist ``result`` and clear ``error_message``."""

    @abstractmethod
    def job_retry_scheduled(
        self, job_id: str, *, attempt: int, error: ErrorDetail, retry_after_seconds: float
    ) -> None:
        """RUNNING -> QUEUED after a retryable failure; keep ``error.message`` as a note."""

    @abstractmethod
    def job_failed(self, job_id: str, *, attempt: int, error: ErrorDetail) -> None:
        """RUNNING -> FAILED; store ``error.message`` in ``error_message``."""
