"""Deterministic mock reviewer — no network, no API keys, no LLM.

It exists so the rest of the team can build and test against a realistic,
contract-compliant reviewer before the real one exists (Week 4), and so failure
handling (retries, FAILED jobs) can be exercised on demand.

Behaviour is selected by :class:`MockScenario`. For a given request the output
is fully deterministic: same ``job_id``/repository/PR/commit give the same
finding ids, files and lines. If the request carries a ``diff`` the findings are
anchored on real added lines from it; otherwise canned file names are used.
"""

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum
from uuid import NAMESPACE_URL, uuid5

from app.reviewers.base import Analysis, Clock, Reviewer
from app.reviewers.diff_utils import AddedLine, parse_added_lines
from app.schemas import (
    Category,
    ErrorCode,
    Finding,
    ReviewAgent,
    ReviewerError,
    ReviewerMode,
    ReviewRequest,
    Severity,
)


class MockScenario(StrEnum):
    """What the mock reviewer does when asked to review."""

    MULTI_AGENT = "multi_agent"  # findings from all four agents (default)
    SINGLE_BUG = "single_bug"  # the one HIGH/BUG finding from the project plan
    CLEAN = "clean"  # no findings: a clean PR
    FAIL_RETRYABLE = "fail_retryable"  # always fails with a retryable error
    FAIL_PERMANENT = "fail_permanent"  # always fails with a non-retryable error
    TIMEOUT = "timeout"  # always fails with REVIEW_TIMEOUT (retryable)
    FLAKY = "flaky"  # fails retryably N times per job, then succeeds


@dataclass(frozen=True)
class _Template:
    """A canned finding; ``file``/``line`` are used when there is no diff."""

    agent: ReviewAgent
    severity: Severity
    category: Category
    message: str
    suggestion: str | None
    confidence: float
    file: str | None
    line: int | None
    end_line: int | None = None
    anchorable: bool = True  # PR-level findings keep file=None even with a diff


_SINGLE_BUG = _Template(
    agent=ReviewAgent.GENERAL,
    severity=Severity.HIGH,
    category=Category.BUG,
    message=(
        "Mock finding: `items[0]` is read before checking that `items` is "
        "non-empty, which raises IndexError for an empty list."
    ),
    suggestion="Check `if not items:` and return early (or raise a clear error).",
    confidence=0.9,
    file="foo.py",
    line=42,
)

# Order matters: it is the order agents "run" and how anchors are spread.
_MULTI_AGENT: tuple[_Template, ...] = (
    _Template(
        ReviewAgent.STATIC_ANALYSIS, Severity.HIGH, Category.BUG,
        "Mock finding: `user` can be None when the lookup misses, but `user.id` "
        "is accessed without a check.",
        "Guard with `if user is None:` and raise a clear error or return 404.",
        0.9, "app/services/users.py", 42,
    ),
    _Template(
        ReviewAgent.STATIC_ANALYSIS, Severity.LOW, Category.MAINTAINABILITY,
        "Mock finding: this block duplicates logic in `format_name()`; extract "
        "a shared helper to keep the two in sync.",
        "Move the shared logic into one function and call it from both places.",
        0.7, "app/utils.py", 17, 24,
    ),
    _Template(
        ReviewAgent.SECURITY, Severity.HIGH, Category.SECURITY,
        "Mock finding: the SQL query is built with string formatting from "
        "request input, which allows SQL injection.",
        "Use a parameterized query and pass the values separately.",
        0.95, "app/db/queries.py", 88,
    ),
    _Template(
        ReviewAgent.SECURITY, Severity.MEDIUM, Category.SECURITY,
        "Mock finding: a value that looks like a hard-coded credential is "
        "committed in source.",
        "Read the secret from the environment or a secrets manager.",
        0.8, "app/config.py", 5,
    ),
    _Template(
        ReviewAgent.ARCHITECTURE, Severity.MEDIUM, Category.ARCHITECTURE,
        "Mock finding: route handlers query the database directly; consider "
        "moving data access behind the service layer to keep layers separable.",
        None, 0.6, None, None, anchorable=False,
    ),
    _Template(
        ReviewAgent.CODE_STYLE, Severity.LOW, Category.STYLE,
        "Mock finding: `getUser` does not follow the repository's snake_case "
        "naming convention.",
        "Rename to `get_user`.",
        0.85, "app/services/users.py", 40,
    ),
)

_ID_NAMESPACE = uuid5(NAMESPACE_URL, "https://github.com/BlazeeeXD/Fidelis/mock-reviewer")


class MockReviewer(Reviewer):
    """Canned reviewer with selectable scenarios (see :class:`MockScenario`)."""

    name = "fidelis-mock-reviewer"
    version = "1.0.0"
    mode = ReviewerMode.MOCK

    def __init__(
        self,
        scenario: MockScenario = MockScenario.MULTI_AGENT,
        *,
        delay_ms: int = 0,
        flaky_failures: int = 2,
        clock: Clock | None = None,
    ) -> None:
        super().__init__(clock=clock)
        if delay_ms < 0:
            raise ValueError("delay_ms must be >= 0")
        if flaky_failures < 0:
            raise ValueError("flaky_failures must be >= 0")
        self.scenario = MockScenario(scenario)
        self.delay_ms = delay_ms
        self.flaky_failures = flaky_failures
        self._calls_per_job: defaultdict[str, int] = defaultdict(int)

    async def analyze(self, request: ReviewRequest) -> Analysis:
        if self.delay_ms:
            await asyncio.sleep(self.delay_ms / 1000)

        self._maybe_fail(request)

        if self.scenario is MockScenario.CLEAN:
            return Analysis(findings=[], agents_run=[ReviewAgent.GENERAL])
        if self.scenario is MockScenario.SINGLE_BUG:
            templates, agents = (_SINGLE_BUG,), [ReviewAgent.GENERAL]
        else:  # MULTI_AGENT, and FLAKY once it has "recovered"
            templates = _MULTI_AGENT
            agents = [
                ReviewAgent.STATIC_ANALYSIS,
                ReviewAgent.SECURITY,
                ReviewAgent.ARCHITECTURE,
                ReviewAgent.CODE_STYLE,
            ]

        anchors = parse_added_lines(request.diff) if request.diff else []
        findings = [
            self._build_finding(request, index, template, anchors, len(templates))
            for index, template in enumerate(templates)
        ]
        return Analysis(findings=findings, agents_run=agents)

    # ------------------------------------------------------------------ #
    def _maybe_fail(self, request: ReviewRequest) -> None:
        scenario = self.scenario
        if scenario is MockScenario.FAIL_RETRYABLE:
            raise ReviewerError(
                ErrorCode.UPSTREAM_ERROR,
                "Mock failure: simulated upstream (GitHub/LLM) error.",
            )
        if scenario is MockScenario.FAIL_PERMANENT:
            raise ReviewerError(
                ErrorCode.INTERNAL_ERROR, "Mock failure: simulated permanent error."
            )
        if scenario is MockScenario.TIMEOUT:
            raise ReviewerError(
                ErrorCode.REVIEW_TIMEOUT, "Mock failure: simulated review timeout."
            )
        if scenario is MockScenario.FLAKY:
            self._calls_per_job[request.job_id] += 1
            attempt = self._calls_per_job[request.job_id]
            if attempt <= self.flaky_failures:
                raise ReviewerError(
                    ErrorCode.UPSTREAM_ERROR,
                    f"Mock failure: flaky reviewer, attempt {attempt} of "
                    f"{self.flaky_failures} failing attempts.",
                    details={"attempt": attempt},
                )

    @staticmethod
    def _build_finding(
        request: ReviewRequest,
        index: int,
        template: _Template,
        anchors: list[AddedLine],
        total: int,
    ) -> Finding:
        file, line, end_line = template.file, template.line, template.end_line
        if anchors and template.anchorable:
            # Spread findings across the diff deterministically.
            anchor = anchors[index * len(anchors) // total]
            file, line, end_line = anchor.file, anchor.line, None

        seed = f"{request.repository}#{request.pr_number}@{request.commit_sha}:{request.job_id}:{index}"
        return Finding(
            id=str(uuid5(_ID_NAMESPACE, seed)),
            agent=template.agent,
            severity=template.severity,
            category=template.category,
            file=file,
            line=line,
            end_line=end_line,
            message=template.message,
            suggestion=template.suggestion,
            confidence=template.confidence,
        )
