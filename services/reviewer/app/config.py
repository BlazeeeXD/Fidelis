"""Environment-driven configuration for the Reviewer Service.

Variables (all optional):

* ``REVIEWER_MODE``            — ``mock`` (default). ``llm`` arrives in Week 4.
* ``REVIEWER_MOCK_SCENARIO``   — a :class:`~app.reviewers.MockScenario` value;
                                 default ``multi_agent``.
* ``REVIEWER_MOCK_DELAY_MS``   — artificial latency per review (default ``0``).
* ``REVIEWER_MOCK_FLAKY_FAILURES`` — failing attempts per job for the ``flaky``
                                 scenario (default ``2``).

Invalid values fail fast at startup with a message naming the variable, rather
than surfacing later as a confusing runtime error.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass

from app.reviewers.mock import MockScenario
from app.schemas import ReviewerMode


class ConfigError(ValueError):
    """Raised when an environment variable has an invalid value."""


@dataclass(frozen=True)
class Settings:
    mode: ReviewerMode = ReviewerMode.MOCK
    mock_scenario: MockScenario = MockScenario.MULTI_AGENT
    mock_delay_ms: int = 0
    mock_flaky_failures: int = 2

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env

        raw_mode = env.get("REVIEWER_MODE", "mock").strip().upper()
        try:
            mode = ReviewerMode(raw_mode)
        except ValueError:
            raise ConfigError(
                f"REVIEWER_MODE={raw_mode!r} is invalid; expected one of "
                f"{[m.value.lower() for m in ReviewerMode]}"
            ) from None

        raw_scenario = env.get("REVIEWER_MOCK_SCENARIO", MockScenario.MULTI_AGENT.value)
        try:
            scenario = MockScenario(raw_scenario.strip().lower())
        except ValueError:
            raise ConfigError(
                f"REVIEWER_MOCK_SCENARIO={raw_scenario!r} is invalid; expected one of "
                f"{[s.value for s in MockScenario]}"
            ) from None

        return cls(
            mode=mode,
            mock_scenario=scenario,
            mock_delay_ms=_non_negative_int(env, "REVIEWER_MOCK_DELAY_MS", 0),
            mock_flaky_failures=_non_negative_int(env, "REVIEWER_MOCK_FLAKY_FAILURES", 2),
        )


def _non_negative_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name}={raw!r} is not an integer") from None
    if value < 0:
        raise ConfigError(f"{name}={raw!r} must be >= 0")
    return value
