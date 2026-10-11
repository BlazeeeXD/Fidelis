"""Pick the reviewer implementation for the current configuration."""

from app.config import Settings
from app.reviewers.base import Reviewer
from app.reviewers.mock import MockReviewer
from app.schemas import ReviewerMode


def create_reviewer(settings: Settings) -> Reviewer:
    """Build the reviewer selected by ``settings.mode``.

    Week 4 adds the LangGraph-backed ``LLM`` reviewer here; nothing else in the
    service needs to change because it only ever talks to the ``Reviewer``
    interface.
    """
    if settings.mode is ReviewerMode.MOCK:
        return MockReviewer(
            scenario=settings.mock_scenario,
            delay_ms=settings.mock_delay_ms,
            flaky_failures=settings.mock_flaky_failures,
        )
    raise NotImplementedError(
        f"Reviewer mode {settings.mode.value!r} is not implemented yet (planned for Week 4)."
    )
