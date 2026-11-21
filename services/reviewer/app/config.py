"""Environment-driven configuration for the Reviewer Service."""
import os
from collections.abc import Mapping
from dataclasses import dataclass

from app.schemas import ReviewAgent, ReviewerMode
from app.reviewers.mock import MockScenario


class ConfigError(ValueError):
    """Raised when an environment variable has an invalid value."""


_DEFAULT_AGENTS = (
    ReviewAgent.STATIC_ANALYSIS,
    ReviewAgent.SECURITY,
    ReviewAgent.ARCHITECTURE,
    ReviewAgent.CODE_STYLE,
)


@dataclass(frozen=True)
class Settings:
    mode: ReviewerMode = ReviewerMode.MOCK
    mock_scenario: MockScenario = MockScenario.MULTI_AGENT
    mock_delay_ms: int = 0
    mock_flaky_failures: int = 2
    # Legacy compatibility; execution no longer depends on this combined knob.
    pipeline_mode: str = "multi_agent"
    review_agents: tuple[ReviewAgent, ...] = _DEFAULT_AGENTS
    execution_strategy: str = "sequential"
    checkpoint_enabled: bool = True
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = 240.0
    # Gemini 3.x defaults to medium thinking; low is faster for PR-review passes.
    llm_reasoning_effort: str = "low"
    llm_temperature: float = 0.1
    llm_max_tokens: int = 6000
    max_diff_chars: int = 120_000
    max_source_context_chars: int = 18_000
    max_context_files: int = 6
    source_context_lines: int = 12
    github_token: str = ""
    github_api_url: str = "https://api.github.com"
    orchestrator_url: str = "http://orchestrator:8000"
    worker_status_token: str = ""

    def __post_init__(self) -> None:
        # Keep direct Settings(...) construction compatible with the old switch.
        if self.review_agents == _DEFAULT_AGENTS and self.pipeline_mode == "single":
            object.__setattr__(self, "review_agents", (ReviewAgent.GENERAL,))
        elif self.review_agents == _DEFAULT_AGENTS and self.pipeline_mode == "security":
            object.__setattr__(self, "review_agents", (ReviewAgent.SECURITY,))

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        raw_mode = env.get("REVIEWER_MODE", "mock").strip().upper()
        try:
            mode = ReviewerMode(raw_mode)
        except ValueError:
            raise ConfigError(
                f"REVIEWER_MODE={raw_mode!r} is invalid; expected one of {[m.value.lower() for m in ReviewerMode]}"
            ) from None
        raw_scenario = env.get("REVIEWER_MOCK_SCENARIO", MockScenario.MULTI_AGENT.value)
        try:
            scenario = MockScenario(raw_scenario.strip().lower())
        except ValueError:
            raise ConfigError(
                f"REVIEWER_MOCK_SCENARIO={raw_scenario!r} is invalid; expected one of {[s.value for s in MockScenario]}"
            ) from None

        legacy_pipeline = env.get("REVIEWER_PIPELINE_MODE", "multi_agent").strip().lower()
        if legacy_pipeline not in {"single", "security", "multi_agent"}:
            raise ConfigError("REVIEWER_PIPELINE_MODE must be 'single', 'security' or 'multi_agent'")
        if "REVIEW_AGENTS" in env:
            review_agents = _parse_agents(env.get("REVIEW_AGENTS", ""))
        elif legacy_pipeline == "single":
            review_agents = (ReviewAgent.GENERAL,)
        elif legacy_pipeline == "security":
            review_agents = (ReviewAgent.SECURITY,)
        else:
            review_agents = _DEFAULT_AGENTS

        strategy = env.get("REVIEW_EXECUTION_STRATEGY", "sequential").strip().lower()
        if strategy not in {"sequential", "single_pass", "parallel"}:
            raise ConfigError("REVIEW_EXECUTION_STRATEGY must be sequential, single_pass or parallel")

        return cls(
            mode=mode,
            mock_scenario=scenario,
            mock_delay_ms=_non_negative_int(env, "REVIEWER_MOCK_DELAY_MS", 0),
            mock_flaky_failures=_non_negative_int(env, "REVIEWER_MOCK_FLAKY_FAILURES", 2),
            pipeline_mode=legacy_pipeline,
            review_agents=review_agents,
            execution_strategy=strategy,
            checkpoint_enabled=_bool(env, "REVIEW_CHECKPOINTS_ENABLED", True),
            llm_base_url=env.get("LLM_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/"),
            llm_api_key=env.get("LLM_API_KEY", "").strip(),
            llm_model=env.get("LLM_MODEL", "gpt-4o-mini").strip(),
            llm_timeout_seconds=_positive_float(env, "LLM_TIMEOUT_SECONDS", 240.0),
            llm_reasoning_effort=_choice(env, "LLM_REASONING_EFFORT", "low", {"minimal", "low", "medium", "high"}),
            llm_temperature=_range_float(env, "LLM_TEMPERATURE", 0.1, 0.0, 2.0),
            llm_max_tokens=_positive_int(env, "LLM_MAX_TOKENS", 6000),
            max_diff_chars=_positive_int(env, "MAX_DIFF_CHARS", 120_000),
            max_source_context_chars=_positive_int(env, "MAX_SOURCE_CONTEXT_CHARS", 18_000),
            max_context_files=_positive_int(env, "MAX_CONTEXT_FILES", 6),
            source_context_lines=_non_negative_int(env, "SOURCE_CONTEXT_LINES", 12),
            github_token=env.get("GITHUB_TOKEN", "").strip(),
            github_api_url=env.get("GITHUB_API_URL", "https://api.github.com").strip().rstrip("/"),
            orchestrator_url=env.get("ORCHESTRATOR_URL", "http://orchestrator:8000").strip().rstrip("/"),
            worker_status_token=env.get("WORKER_STATUS_TOKEN", "").strip(),
        )


def _parse_agents(raw: str) -> tuple[ReviewAgent, ...]:
    values = [item.strip().upper() for item in raw.split(",") if item.strip()]
    if not values:
        raise ConfigError("REVIEW_AGENTS must contain at least one agent")
    agents: list[ReviewAgent] = []
    for value in values:
        try:
            agent = ReviewAgent(value)
        except ValueError:
            raise ConfigError(f"REVIEW_AGENTS contains unsupported agent {value!r}") from None
        if agent in agents:
            raise ConfigError(f"REVIEW_AGENTS contains duplicate agent {value!r}")
        agents.append(agent)
    return tuple(agents)


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name}={raw!r} must be a boolean (true/false)")


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


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    value = _non_negative_int(env, name, default)
    if value < 1:
        raise ConfigError(f"{name} must be >= 1")
    return value


def _positive_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name}={raw!r} is not a number") from None
    if value <= 0:
        raise ConfigError(f"{name} must be > 0")
    return value


def _range_float(env: Mapping[str, str], name: str, default: float, low: float, high: float) -> float:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name}={raw!r} is not a number") from None
    if not low <= value <= high:
        raise ConfigError(f"{name} must be between {low} and {high}")
    return value


def _choice(env: Mapping[str, str], name: str, default: str, allowed: set[str]) -> str:
    raw = env.get(name, default).strip().lower()
    if raw not in allowed:
        raise ConfigError(f"{name}={raw!r} must be one of {sorted(allowed)}")
    return raw
