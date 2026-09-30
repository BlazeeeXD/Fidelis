"""Domain model for a review job.

This is the in-memory / domain representation for Week 1. P4 will
formalize this as a SQLAlchemy model against Postgres later (Week 2+);
this module's shape is exactly what that model should mirror, per the
contract doc.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4


class JobStatus(str, enum.Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass
class ReviewJob:
    repository: str
    pr_number: int
    commit_sha: str
    id: str = field(default_factory=lambda: str(uuid4()))
    status: JobStatus = JobStatus.QUEUED
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_message: str | None = None