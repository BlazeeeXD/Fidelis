import pytest
from app.config import ConfigError, Settings
from app.reviewers import MockReviewer, MockScenario
from app.reviewers.factory import create_reviewer
from app.reviewers.llm import LLMReviewer
from app.schemas import ReviewerMode


def test_defaults():
    s = Settings.from_env({})
    assert s.mode is ReviewerMode.MOCK
    assert s.mock_scenario is MockScenario.MULTI_AGENT
    assert (s.mock_delay_ms, s.mock_flaky_failures) == (0, 2)


def test_reads_all_variables_case_insensitively():
    s = Settings.from_env({
        "REVIEWER_MODE": "Mock",
        "REVIEWER_MOCK_SCENARIO": "FLAKY",
        "REVIEWER_MOCK_DELAY_MS": "250",
        "REVIEWER_MOCK_FLAKY_FAILURES": "4",
    })
    assert s.mock_scenario is MockScenario.FLAKY
    assert (s.mock_delay_ms, s.mock_flaky_failures) == (250, 4)


def test_blank_numeric_values_use_defaults():
    assert Settings.from_env({"REVIEWER_MOCK_DELAY_MS": " "}).mock_delay_ms == 0


@pytest.mark.parametrize(
    ("env", "needle"),
    [
        ({"REVIEWER_MODE": "gpt"}, "REVIEWER_MODE"),
        ({"REVIEWER_MOCK_SCENARIO": "chaos"}, "REVIEWER_MOCK_SCENARIO"),
        ({"REVIEWER_MOCK_DELAY_MS": "abc"}, "REVIEWER_MOCK_DELAY_MS"),
        ({"REVIEWER_MOCK_DELAY_MS": "-5"}, "REVIEWER_MOCK_DELAY_MS"),
        ({"REVIEWER_MOCK_FLAKY_FAILURES": "-1"}, "REVIEWER_MOCK_FLAKY_FAILURES"),
        ({"REVIEWER_PIPELINE_MODE": "unknown"}, "REVIEWER_PIPELINE_MODE"),
    ],
)
def test_invalid_values_fail_fast_naming_the_variable(env, needle):
    with pytest.raises(ConfigError, match=needle):
        Settings.from_env(env)


def test_factory_builds_configured_mock():
    reviewer = create_reviewer(Settings(mock_scenario=MockScenario.CLEAN, mock_delay_ms=5))
    assert isinstance(reviewer, MockReviewer)
    assert reviewer.scenario is MockScenario.CLEAN and reviewer.delay_ms == 5


def test_factory_builds_configured_llm_reviewer():
    reviewer = create_reviewer(Settings(mode=ReviewerMode.LLM))
    assert isinstance(reviewer, LLMReviewer)
    assert reviewer.settings.pipeline_mode == "multi_agent"


def test_security_pipeline_is_a_supported_lightweight_specialist_mode():
    settings = Settings.from_env({"REVIEWER_MODE": "llm", "REVIEWER_PIPELINE_MODE": "security"})
    reviewer = create_reviewer(settings)
    assert isinstance(reviewer, LLMReviewer)
    assert [agent.value for agent in reviewer._agents] == ["SECURITY"]


def test_agent_selection_is_independent_from_execution_strategy():
    settings = Settings.from_env({
        "REVIEWER_MODE": "llm",
        "REVIEW_AGENTS": "SECURITY,STATIC_ANALYSIS",
        "REVIEW_EXECUTION_STRATEGY": "sequential",
    })
    assert [agent.value for agent in settings.review_agents] == ["SECURITY", "STATIC_ANALYSIS"]
    assert settings.execution_strategy == "sequential"


def test_single_pass_is_a_supported_execution_strategy():
    settings = Settings.from_env({"REVIEW_EXECUTION_STRATEGY": "single_pass"})
    assert settings.execution_strategy == "single_pass"


def test_duplicate_review_agents_are_rejected():
    with pytest.raises(ConfigError, match="duplicate agent"):
        Settings.from_env({"REVIEW_AGENTS": "SECURITY,SECURITY"})


def test_gemini_reasoning_effort_defaults_to_low():
    settings = Settings.from_env({"LLM_BASE_URL": "https://generativelanguage.googleapis.com/v1beta/openai"})
    assert settings.llm_reasoning_effort == "low"


def test_gemini_reasoning_effort_can_be_configured():
    settings = Settings.from_env({"LLM_REASONING_EFFORT": "high"})
    assert settings.llm_reasoning_effort == "high"


def test_gemini_flash_lite_minimal_reasoning_is_supported():
    settings = Settings.from_env({"LLM_REASONING_EFFORT": "minimal"})
    assert settings.llm_reasoning_effort == "minimal"


def test_invalid_gemini_reasoning_effort_fails_fast():
    with pytest.raises(ConfigError, match="LLM_REASONING_EFFORT"):
        Settings.from_env({"LLM_REASONING_EFFORT": "none"})
