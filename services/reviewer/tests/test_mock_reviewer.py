import asyncio

import pytest
from app.reviewers import MockReviewer, MockScenario, Reviewer
from app.schemas import (
    Category,
    ErrorCode,
    ReviewAgent,
    ReviewerError,
    ReviewerMode,
    ReviewRequest,
    ReviewResult,
    Severity,
)

from tests.conftest import SAMPLE_DIFF


def run(reviewer: Reviewer, request: ReviewRequest) -> ReviewResult:
    return asyncio.run(reviewer.review(request))


def test_mock_is_a_reviewer_and_identifies_itself():
    reviewer = MockReviewer()
    assert isinstance(reviewer, Reviewer)
    info = reviewer.info()
    assert info.mode is ReviewerMode.MOCK and info.name and info.version


def test_default_scenario_covers_all_four_agents(request_obj, clock):
    result = run(MockReviewer(clock=clock), request_obj)

    assert result.agents_run == [
        ReviewAgent.STATIC_ANALYSIS,
        ReviewAgent.SECURITY,
        ReviewAgent.ARCHITECTURE,
        ReviewAgent.CODE_STYLE,
    ]
    assert {f.agent for f in result.findings} == set(result.agents_run)
    assert {f.severity for f in result.findings} == set(Severity)
    assert {f.category for f in result.findings} == set(Category)
    assert result.summary.total_findings == len(result.findings) == 6
    assert result.reviewer.mode is ReviewerMode.MOCK
    assert result.duration_ms == 250


def test_architecture_finding_is_pr_level(request_obj):
    result = run(MockReviewer(), request_obj)
    arch = next(f for f in result.findings if f.agent is ReviewAgent.ARCHITECTURE)
    assert arch.file is None and arch.line is None


def test_output_is_deterministic_for_same_request(request_obj):
    first = run(MockReviewer(), request_obj)
    second = run(MockReviewer(), request_obj)
    assert [f.model_dump() for f in first.findings] == [f.model_dump() for f in second.findings]


def test_finding_ids_differ_between_jobs(request_obj):
    other = request_obj.model_copy(update={"job_id": "job-2"})
    ids_a = {f.id for f in run(MockReviewer(), request_obj).findings}
    ids_b = {f.id for f in run(MockReviewer(), other).findings}
    assert ids_a.isdisjoint(ids_b)


def test_finding_ids_are_unique_within_a_result(request_obj):
    ids = [f.id for f in run(MockReviewer(), request_obj).findings]
    assert len(ids) == len(set(ids))


def test_single_bug_matches_the_plan_example(request_obj):
    result = run(MockReviewer(MockScenario.SINGLE_BUG), request_obj)
    assert result.agents_run == [ReviewAgent.GENERAL]
    (finding,) = result.findings
    assert (finding.severity, finding.category, finding.file, finding.line) == (
        Severity.HIGH, Category.BUG, "foo.py", 42,
    )


def test_clean_scenario_has_no_findings(request_obj):
    result = run(MockReviewer(MockScenario.CLEAN), request_obj)
    assert result.findings == []
    assert result.summary.total_findings == 0
    assert result.summary.highest_severity is None


def test_findings_anchor_on_real_diff_lines(request_obj):
    request = request_obj.model_copy(update={"diff": SAMPLE_DIFF})
    result = run(MockReviewer(), request)

    valid = {("app/users.py", 11), ("app/users.py", 12),
             ("app/db.py", 1), ("app/db.py", 2), ("app/db.py", 3)}
    located = [f for f in result.findings if f.file is not None]
    assert located, "expected at least one located finding"
    assert all((f.file, f.line) in valid for f in located)
    # PR-level findings stay PR-level even when a diff is supplied.
    assert any(f.file is None for f in result.findings)


def test_diff_without_added_lines_falls_back_to_canned_locations(request_obj):
    request = request_obj.model_copy(update={"diff": "nothing useful"})
    result = run(MockReviewer(), request)
    assert any(f.file == "app/services/users.py" for f in result.findings)


@pytest.mark.parametrize(
    ("scenario", "code", "retryable"),
    [
        (MockScenario.FAIL_RETRYABLE, ErrorCode.UPSTREAM_ERROR, True),
        (MockScenario.FAIL_PERMANENT, ErrorCode.INTERNAL_ERROR, False),
        (MockScenario.TIMEOUT, ErrorCode.REVIEW_TIMEOUT, True),
    ],
)
def test_failure_scenarios(request_obj, scenario, code, retryable):
    with pytest.raises(ReviewerError) as excinfo:
        run(MockReviewer(scenario), request_obj)
    assert excinfo.value.code is code
    assert excinfo.value.retryable is retryable


def test_flaky_fails_n_times_per_job_then_succeeds(request_obj):
    reviewer = MockReviewer(MockScenario.FLAKY, flaky_failures=2)
    for attempt in (1, 2):
        with pytest.raises(ReviewerError) as excinfo:
            run(reviewer, request_obj)
        assert excinfo.value.retryable is True
        assert excinfo.value.details == {"attempt": attempt}
    assert run(reviewer, request_obj).summary.total_findings == 6
    # Still succeeds afterwards.
    assert run(reviewer, request_obj).summary.total_findings == 6


def test_flaky_counts_attempts_independently_per_job(request_obj):
    reviewer = MockReviewer(MockScenario.FLAKY, flaky_failures=1)
    other = request_obj.model_copy(update={"job_id": "other"})
    with pytest.raises(ReviewerError):
        run(reviewer, request_obj)
    with pytest.raises(ReviewerError):  # other job has its own counter
        run(reviewer, other)
    assert run(reviewer, request_obj).findings
    assert run(reviewer, other).findings


def test_flaky_with_zero_failures_never_fails(request_obj):
    assert run(MockReviewer(MockScenario.FLAKY, flaky_failures=0), request_obj).findings


def test_delay_is_applied(request_obj, monkeypatch):
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    run(MockReviewer(delay_ms=1500), request_obj)
    assert slept == [1.5]


def test_invalid_constructor_arguments():
    with pytest.raises(ValueError, match="delay_ms"):
        MockReviewer(delay_ms=-1)
    with pytest.raises(ValueError, match="flaky_failures"):
        MockReviewer(flaky_failures=-1)
    with pytest.raises(ValueError):
        MockReviewer(scenario="nope")


def test_scenarios_accept_plain_strings(request_obj):
    assert run(MockReviewer("clean"), request_obj).findings == []
