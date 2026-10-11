import pytest
from app.config import ConfigError, Settings
from app.reviewers import MockReviewer, MockScenario
from app.reviewers.factory import create_reviewer
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
    ],
)
def test_invalid_values_fail_fast_naming_the_variable(env, needle):
    with pytest.raises(ConfigError, match=needle):
        Settings.from_env(env)


def test_factory_builds_configured_mock():
    reviewer = create_reviewer(Settings(mock_scenario=MockScenario.CLEAN, mock_delay_ms=5))
    assert isinstance(reviewer, MockReviewer)
    assert reviewer.scenario is MockScenario.CLEAN and reviewer.delay_ms == 5


def test_factory_rejects_llm_mode_until_week_4():
    with pytest.raises(NotImplementedError):
        create_reviewer(Settings(mode=ReviewerMode.LLM))
