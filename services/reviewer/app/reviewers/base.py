"""The reviewer interface every implementation must satisfy.

Contract for implementers (the mock today, the LangGraph pipeline in Week 4):

* Subclass :class:`Reviewer`, set ``name`` / ``version`` / ``mode`` and
  implement :meth:`Reviewer.analyze`. Return the findings and which agents ran.
* Do **not** build :class:`~app.schemas.ReviewResult` yourself — :meth:`review`
  does that, so ordering, summary and timing are identical for every reviewer.
* Signal expected failures by raising :class:`~app.schemas.ReviewerError` with
  the right code; the API and the worker translate it for callers. Anything else
  that escapes is treated as an internal bug.
* ``analyze`` is ``async`` because real reviewers are I/O bound (GitHub + LLM
  calls) and the multi-agent graph will run agents concurrently.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import ClassVar

from app.schemas import (
    Finding,
    ReviewAgent,
    ReviewerInfo,
    ReviewerMode,
    ReviewRequest,
    ReviewResult,
)

Clock = Callable[[], datetime]


def utcnow() -> datetime:
    """Timezone-aware current time (UTC)."""
    return datetime.now(UTC)


@dataclass(frozen=True)
class Analysis:
    """What a reviewer's analysis step produces, before packaging."""

    findings: list[Finding] = field(default_factory=list)
    agents_run: list[ReviewAgent] = field(default_factory=list)


class Reviewer(ABC):
    """Reviews one PR commit and returns a structured, prioritized result."""

    name: ClassVar[str]
    version: ClassVar[str]
    mode: ClassVar[ReviewerMode]

    def __init__(self, clock: Clock | None = None) -> None:
        # Injectable so tests get deterministic timestamps.
        self._clock: Clock = clock or utcnow

    @abstractmethod
    async def analyze(self, request: ReviewRequest) -> Analysis:
        """Produce findings for ``request``. May raise ``ReviewerError``."""

    async def review(self, request: ReviewRequest) -> ReviewResult:
        """Run the analysis and package it as a validated ``ReviewResult``."""
        started_at = self._clock()
        analysis = await self.analyze(request)
        completed_at = self._clock()
        return ReviewResult.create(
            request=request,
            reviewer=self.info(),
            agents_run=analysis.agents_run,
            findings=analysis.findings,
            started_at=started_at,
            completed_at=completed_at,
        )

    def info(self) -> ReviewerInfo:
        return ReviewerInfo(name=self.name, version=self.version, mode=self.mode)
