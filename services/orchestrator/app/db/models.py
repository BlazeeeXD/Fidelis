"""SQLAlchemy models for jobs, durable review results, audit history and learned rules."""
from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base
from app.models.job import JobStatus


class ReviewJobDB(Base):
    """PostgreSQL representation of a review job."""

    __tablename__ = "review_jobs"
    __table_args__ = (
        Index(
            "uq_review_jobs_active_repo_pr_sha",
            "repository",
            "pr_number",
            "commit_sha",
            unique=True,
            sqlite_where=text("status IN ('QUEUED', 'RUNNING')"),
            postgresql_where=text("status IN ('QUEUED', 'RUNNING')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    repository: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    commit_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, native_enum=False, length=9), nullable=False, default=JobStatus.QUEUED
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class ReviewJobOutboxDB(Base):
    """Transactional outbox row guaranteeing a persisted job can be dispatched."""

    __tablename__ = "review_job_outbox"

    job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("review_jobs.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReviewResultDB(Base):
    """One canonical structured result per successfully reviewed job."""

    __tablename__ = "review_results"

    job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("review_jobs.id", ondelete="CASCADE"), primary_key=True
    )
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    reviewer_name: Mapped[str] = mapped_column(String(128), nullable=False)
    reviewer_version: Mapped[str] = mapped_column(String(32), nullable=False)
    reviewer_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)


class ReviewFindingDB(Base):
    """Queryable finding row; the complete result remains in ReviewResultDB.payload."""

    __tablename__ = "review_findings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("review_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    file: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    suggestion: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)


class ReviewAuditDB(Base):
    """Append-only lifecycle and delivery audit trail for each review job."""

    __tablename__ = "review_audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("review_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    from_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    attempt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class RepositoryRuleDB(Base):
    """A mined convention that can be injected into future review prompts."""

    __tablename__ = "repository_rules"
    __table_args__ = (
        UniqueConstraint("repository", "rule_key", name="uq_repository_rule_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    repository: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    rule_key: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    evidence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    source_prs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ReviewAgentRunDB(Base):
    """Per-agent timings and token usage for operational analysis."""

    __tablename__ = "review_agent_runs"
    __table_args__ = (UniqueConstraint("job_id", "agent", name="uq_review_agent_run"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("review_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent: Mapped[str] = mapped_column(String(32), nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    findings_count: Mapped[int] = mapped_column(Integer, nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)


class ReviewAgentCheckpointDB(Base):
    """Durable per-agent result checkpoint, reused when a review attempt retries."""

    __tablename__ = "review_agent_checkpoints"
    __table_args__ = (UniqueConstraint("job_id", "agent", name="uq_review_agent_checkpoint"),)

    job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("review_jobs.id", ondelete="CASCADE"), primary_key=True
    )
    agent: Mapped[str] = mapped_column(String(32), primary_key=True)
    context_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
