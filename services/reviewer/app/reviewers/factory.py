"""Pick the reviewer implementation for the current configuration."""
from app.config import Settings
from app.reviewers.base import Reviewer
from app.reviewers.llm import LLMReviewer
from app.reviewers.mock import MockReviewer
from app.schemas import ReviewerMode


def create_reviewer(settings: Settings) -> Reviewer:
    if settings.mode is ReviewerMode.MOCK:
        return MockReviewer(
            scenario=settings.mock_scenario,
            delay_ms=settings.mock_delay_ms,
            flaky_failures=settings.mock_flaky_failures,
        )
    if settings.mode is ReviewerMode.LLM:
        return LLMReviewer(settings)
    raise ValueError(f"Unsupported reviewer mode: {settings.mode}")
