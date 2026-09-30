"""SQLAlchemy ORM models for the Orchestrator service."""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base
from app.models.job import JobStatus


class ReviewJobDB(Base):
    """PostgreSQL representation of a review job."""

    __tablename__ = "review_jobs"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
    )

    repository: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    pr_number: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    commit_sha: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )

    status: Mapped[JobStatus] = mapped_column(
        nullable=False,
        default=JobStatus.QUEUED,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    error_message: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
    )