"""Reviewer output/input schema — contract version ``1.0``.

This module is the single source of truth for what flows in and out of the
Reviewer Service:

* :class:`ReviewRequest`  — what the orchestrator / Celery worker sends.
* :class:`ReviewResult`   — what comes back: a prioritized list of
  :class:`Finding` objects plus a consistent :class:`ReviewSummary`.

Design rules (see ``docs/contracts/reviewer-contract.md``):

* Every model forbids unknown fields, so typos and silent drift are caught at
  the service boundary instead of three services later.
* Enums are closed sets. Adding a value is a contract change: bump
  :data:`SCHEMA_VERSION`, update the docs and tell the team.
* ``severity`` and ``category`` use the exact vocabulary from the project plan
  (``HIGH``/``MEDIUM``/``LOW``, ``BUG``/``SECURITY``/...), so P4 (database) and
  P5 (metrics) can group on them directly.
"""

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "1.0"

# "owner/repo" exactly as GitHub spells it (letters, digits, '.', '_', '-').
REPOSITORY_PATTERN = r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"


class _StrictModel(BaseModel):
    """Base for all contract models: unknown fields are an error."""

    model_config = ConfigDict(extra="forbid")


class Severity(StrEnum):
    """How urgently a developer should act. Ordered HIGH > MEDIUM > LOW."""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"

    @property
    def rank(self) -> int:
        """Numeric weight for sorting (higher = more severe)."""
        return {"HIGH": 3, "MEDIUM": 2, "LOW": 1}[self.value]


class Category(StrEnum):
    """What kind of problem a finding describes.

    Covers the concerns listed in the project synopsis: bugs/runtime issues,
    security vulnerabilities, architectural decisions, duplication and
    maintainability, and style/convention violations.
    """

    BUG = "BUG"
    SECURITY = "SECURITY"
    ARCHITECTURE = "ARCHITECTURE"
    MAINTAINABILITY = "MAINTAINABILITY"
    STYLE = "STYLE"


class ReviewAgent(StrEnum):
    """Which agent produced a finding.

    The four specialised agents come from the synopsis. ``GENERAL`` is the
    single all-purpose reviewer used in the V3 milestone (one AI reviewer)
    before the multi-agent split lands.
    """

    STATIC_ANALYSIS = "STATIC_ANALYSIS"
    SECURITY = "SECURITY"
    ARCHITECTURE = "ARCHITECTURE"
    CODE_STYLE = "CODE_STYLE"
    GENERAL = "GENERAL"


class ReviewerMode(StrEnum):
    """Whether results come from the canned mock or a real LLM pipeline."""

    MOCK = "MOCK"
    LLM = "LLM"


# --------------------------------------------------------------------------- #
# Finding
# --------------------------------------------------------------------------- #
class Finding(_StrictModel):
    """A single, actionable review comment.

    The core five fields from the project plan are ``severity``, ``category``,
    ``file``, ``line`` and ``message``; the rest make findings traceable
    (``id``, ``agent``) and actionable (``suggestion``, ``end_line``) and let
    the aggregator weigh them (``confidence``).

    ``file`` and ``line`` are optional because some findings are PR-level
    (e.g. an architecture concern spanning many files). Rules enforced:
    ``line`` needs ``file``; ``end_line`` needs ``line`` and must not precede it.
    """

    id: str = Field(
        default_factory=lambda: str(uuid4()),
        min_length=1,
        description="Unique id of this finding (stable across re-serialisation).",
    )
    agent: ReviewAgent
    severity: Severity
    category: Category
    file: str | None = Field(
        default=None,
        description="Repo-relative path using '/' separators; null for PR-level findings.",
        examples=["src/app/auth.py"],
    )
    line: int | None = Field(
        default=None, ge=1, description="1-based line in the new version of the file."
    )
    end_line: int | None = Field(
        default=None, ge=1, description="Last line of a multi-line finding (inclusive)."
    )
    message: str = Field(min_length=1, max_length=4000)
    suggestion: str | None = Field(
        default=None, max_length=4000, description="Concrete fix, if one is known."
    )
    confidence: float = Field(
        default=1.0, ge=0.0, le=1.0, description="Agent's confidence, 0.0–1.0."
    )

    @field_validator("file")
    @classmethod
    def _normalise_file(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip().removeprefix("./")
        if not cleaned:
            raise ValueError("file must not be empty (use null for PR-level findings)")
        if cleaned.startswith("/") or "\\" in cleaned:
            raise ValueError("file must be a repo-relative path with '/' separators")
        if ".." in cleaned.split("/"):
            raise ValueError("file must not contain '..' segments")
        return cleaned

    @field_validator("message")
    @classmethod
    def _clean_message(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("message must not be blank")
        return stripped

    @field_validator("suggestion")
    @classmethod
    def _clean_suggestion(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @model_validator(mode="after")
    def _check_location(self) -> "Finding":
        if self.line is not None and self.file is None:
            raise ValueError("line requires file")
        if self.end_line is not None:
            if self.line is None:
                raise ValueError("end_line requires line")
            if self.end_line < self.line:
                raise ValueError("end_line must be >= line")
        return self

    @property
    def sort_key(self) -> tuple:
        """Priority order: severity desc, then file, line, agent, id.

        Findings without a file sort after located ones within a severity.
        """
        return (
            -self.severity.rank,
            self.file is None,
            self.file or "",
            self.line or 0,
            self.agent.value,
            self.id,
        )


# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #
class ReviewSummary(_StrictModel):
    """Counts derived from ``findings``; always consistent with them.

    Every enum value appears as a key (with ``0`` when absent) so consumers —
    the dashboard metrics in particular — never need to handle missing keys.
    """

    total_findings: int = Field(ge=0)
    by_severity: dict[Severity, int]
    by_category: dict[Category, int]
    by_agent: dict[ReviewAgent, int]
    highest_severity: Severity | None = Field(
        description="Most severe finding present; null when there are no findings."
    )

    @classmethod
    def from_findings(cls, findings: list[Finding]) -> "ReviewSummary":
        by_severity = dict.fromkeys(Severity, 0)
        by_category = dict.fromkeys(Category, 0)
        by_agent = dict.fromkeys(ReviewAgent, 0)
        for finding in findings:
            by_severity[finding.severity] += 1
            by_category[finding.category] += 1
            by_agent[finding.agent] += 1
        highest = max(
            (f.severity for f in findings), key=lambda s: s.rank, default=None
        )
        return cls(
            total_findings=len(findings),
            by_severity=by_severity,
            by_category=by_category,
            by_agent=by_agent,
            highest_severity=highest,
        )


# --------------------------------------------------------------------------- #
# Request
# --------------------------------------------------------------------------- #
class ReviewRequest(_StrictModel):
    """Ask the reviewer to review one PR at one commit.

    ``repository``, ``pr_number`` and ``commit_sha`` are the same fields the
    orchestrator already stores on a review job (see
    ``docs/contracts/review-job-contract.md``); ``job_id`` ties the result back
    to that job. ``diff`` is optional: when omitted the real reviewer fetches
    the diff from GitHub itself (Week 4); when supplied it is used as-is, which
    keeps tests and mock runs independent of the network.
    """

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    job_id: str = Field(min_length=1, max_length=64)
    repository: str = Field(pattern=REPOSITORY_PATTERN, examples=["owner/repo"])
    pr_number: int = Field(gt=0)
    commit_sha: str = Field(min_length=1, max_length=64)
    diff: str | None = Field(
        default=None,
        max_length=2_000_000,
        description="Unified diff of the PR. Optional; fetched from GitHub if absent.",
    )


# --------------------------------------------------------------------------- #
# Result
# --------------------------------------------------------------------------- #
class ReviewerInfo(_StrictModel):
    """Identifies which reviewer implementation produced a result."""

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    mode: ReviewerMode


class ReviewResult(_StrictModel):
    """The complete, prioritized review for one PR commit.

    Build instances with :meth:`create` — it sorts findings by priority and
    computes ``summary`` and ``duration_ms`` so they cannot disagree with the
    data. Parsing validates that they do agree, so a hand-edited or corrupted
    payload is rejected rather than trusted.
    """

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    job_id: str = Field(min_length=1, max_length=64)
    repository: str = Field(pattern=REPOSITORY_PATTERN)
    pr_number: int = Field(gt=0)
    commit_sha: str = Field(min_length=1, max_length=64)
    reviewer: ReviewerInfo
    agents_run: list[ReviewAgent] = Field(
        description="Agents that actually ran for this review, in execution order."
    )
    findings: list[Finding]
    summary: ReviewSummary
    started_at: datetime
    completed_at: datetime
    duration_ms: int = Field(ge=0)

    @classmethod
    def create(
        cls,
        *,
        request: ReviewRequest,
        reviewer: ReviewerInfo,
        agents_run: list[ReviewAgent],
        findings: list[Finding],
        started_at: datetime,
        completed_at: datetime,
    ) -> "ReviewResult":
        """Assemble a result: sort findings, compute summary and duration."""
        ordered = sorted(findings, key=lambda f: f.sort_key)
        duration_ms = 0
        if started_at.tzinfo is not None and completed_at.tzinfo is not None:
            duration_ms = max(0, round((completed_at - started_at).total_seconds() * 1000))
        # Naive timestamps skip the subtraction (it would raise TypeError); the
        # model validators below then reject them with a proper ValidationError.
        return cls(
            job_id=request.job_id,
            repository=request.repository,
            pr_number=request.pr_number,
            commit_sha=request.commit_sha,
            reviewer=reviewer,
            agents_run=agents_run,
            findings=ordered,
            summary=ReviewSummary.from_findings(ordered),
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=duration_ms,
        )

    @field_validator("started_at", "completed_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware (use UTC)")
        return value

    @model_validator(mode="after")
    def _check_consistency(self) -> "ReviewResult":
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must not precede started_at")
        if self.summary != ReviewSummary.from_findings(self.findings):
            raise ValueError("summary does not match findings")
        expected_order = sorted(self.findings, key=lambda f: f.sort_key)
        if self.findings != expected_order:
            raise ValueError("findings must be sorted by priority (see Finding.sort_key)")
        return self
