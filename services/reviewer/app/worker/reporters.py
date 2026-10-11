"""Reference implementations of :class:`JobStatusReporter`."""

import logging
from dataclasses import dataclass, field
from datetime import datetime

from app.reviewers.base import Clock, utcnow
from app.schemas import ErrorDetail, ReviewResult
from app.worker.interface import (
    ALLOWED_TRANSITIONS,
    InvalidTransitionError,
    JobStatus,
    JobStatusReporter,
    UnknownJobError,
)

logger = logging.getLogger("reviewer.worker")


@dataclass
class JobRecord:
    """In-memory twin of the orchestrator's ``ReviewJob`` (+ the result)."""

    job_id: str
    status: JobStatus = JobStatus.QUEUED
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_message: str | None = None
    attempts: int = 0
    result: ReviewResult | None = None


@dataclass(frozen=True)
class StatusChange:
    """One entry in the audit trail kept by :class:`InMemoryStatusReporter`."""

    job_id: str
    from_status: JobStatus
    to_status: JobStatus
    at: datetime
    attempt: int | None = None
    error: ErrorDetail | None = None
    retry_after_seconds: float | None = None


@dataclass
class InMemoryStatusReporter(JobStatusReporter):
    """Reporter backed by a dict; enforces the status state machine.

    Use it in unit tests and the mock execution. It is also a readable
    specification for P3/P4: the real reporter must behave the same way, with a
    database underneath.
    """

    clock: Clock = utcnow
    jobs: dict[str, JobRecord] = field(default_factory=dict)
    history: list[StatusChange] = field(default_factory=list)

    def add_job(self, job_id: str) -> JobRecord:
        """Register a freshly created job in QUEUED (what the orchestrator does)."""
        if job_id in self.jobs:
            raise ValueError(f"job {job_id} already exists")
        record = JobRecord(job_id=job_id)
        self.jobs[job_id] = record
        return record

    def status_of(self, job_id: str) -> JobStatus:
        return self._get(job_id).status

    # -- JobStatusReporter ------------------------------------------------ #
    def job_started(self, job_id: str, *, attempt: int) -> None:
        record = self._transition(job_id, JobStatus.RUNNING, attempt=attempt)
        record.attempts = attempt
        if record.started_at is None:
            record.started_at = self.clock()

    def job_completed(self, job_id: str, result: ReviewResult) -> None:
        record = self._transition(job_id, JobStatus.COMPLETED)
        record.completed_at = self.clock()
        record.error_message = None
        record.result = result

    def job_retry_scheduled(
        self, job_id: str, *, attempt: int, error: ErrorDetail, retry_after_seconds: float
    ) -> None:
        record = self._transition(
            job_id,
            JobStatus.QUEUED,
            attempt=attempt,
            error=error,
            retry_after_seconds=retry_after_seconds,
        )
        record.error_message = error.message

    def job_failed(self, job_id: str, *, attempt: int, error: ErrorDetail) -> None:
        record = self._transition(job_id, JobStatus.FAILED, attempt=attempt, error=error)
        record.completed_at = self.clock()
        record.error_message = error.message

    # -- internals -------------------------------------------------------- #
    def _get(self, job_id: str) -> JobRecord:
        try:
            return self.jobs[job_id]
        except KeyError:
            raise UnknownJobError(job_id) from None

    def _transition(
        self,
        job_id: str,
        target: JobStatus,
        *,
        attempt: int | None = None,
        error: ErrorDetail | None = None,
        retry_after_seconds: float | None = None,
    ) -> JobRecord:
        record = self._get(job_id)
        if target not in ALLOWED_TRANSITIONS[record.status]:
            raise InvalidTransitionError(job_id, record.status, target)
        self.history.append(
            StatusChange(
                job_id=job_id,
                from_status=record.status,
                to_status=target,
                at=self.clock(),
                attempt=attempt,
                error=error,
                retry_after_seconds=retry_after_seconds,
            )
        )
        record.status = target
        return record


class LoggingStatusReporter(JobStatusReporter):
    """Reporter that only logs. Handy for running a worker with no database."""

    def job_started(self, job_id: str, *, attempt: int) -> None:
        logger.info("job %s: RUNNING (attempt %s)", job_id, attempt)

    def job_completed(self, job_id: str, result: ReviewResult) -> None:
        logger.info(
            "job %s: COMPLETED (%s finding(s))", job_id, result.summary.total_findings
        )

    def job_retry_scheduled(
        self, job_id: str, *, attempt: int, error: ErrorDetail, retry_after_seconds: float
    ) -> None:
        logger.warning(
            "job %s: QUEUED for retry in %.1fs after attempt %s: %s %s",
            job_id, retry_after_seconds, attempt, error.code.value, error.message,
        )

    def job_failed(self, job_id: str, *, attempt: int, error: ErrorDetail) -> None:
        logger.error(
            "job %s: FAILED on attempt %s: %s %s", job_id, attempt, error.code.value, error.message
        )
