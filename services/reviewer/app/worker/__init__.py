"""Review-worker interface and reference implementations (Week 3).

See ``docs/contracts/review-worker-interface.md``.
"""

from app.worker.clients import HttpReviewerClient, InProcessReviewerClient
from app.worker.interface import (
    ALLOWED_TRANSITIONS,
    ExecutionDisposition,
    ExecutionOutcome,
    InvalidTransitionError,
    JobStatus,
    JobStatusReporter,
    ReviewerClient,
    UnknownJobError,
)
from app.worker.mock_execution import MockExecutionReport, run_mock_execution
from app.worker.reporters import (
    HttpJobStatusReporter,
    InMemoryStatusReporter,
    JobRecord,
    LoggingStatusReporter,
    StatusChange,
    StatusReporterRejected,
    StatusReporterUnavailable,
)
from app.worker.runner import ReviewWorker, backoff_seconds

__all__ = [
    "ALLOWED_TRANSITIONS",
    "ExecutionDisposition",
    "ExecutionOutcome",
    "HttpJobStatusReporter",
    "HttpReviewerClient",
    "InMemoryStatusReporter",
    "InProcessReviewerClient",
    "InvalidTransitionError",
    "JobRecord",
    "JobStatus",
    "JobStatusReporter",
    "LoggingStatusReporter",
    "MockExecutionReport",
    "ReviewWorker",
    "ReviewerClient",
    "StatusChange",
    "StatusReporterRejected",
    "StatusReporterUnavailable",
    "UnknownJobError",
    "backoff_seconds",
    "run_mock_execution",
]
