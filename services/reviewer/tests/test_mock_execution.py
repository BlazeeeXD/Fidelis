import pytest
from app.reviewers import MockScenario
from app.schemas import ReviewRequest
from app.worker import JobStatus, run_mock_execution
from app.worker.demo import main as demo_main


def test_happy_path():
    report = run_mock_execution()
    assert report.final_status is JobStatus.COMPLETED
    assert report.transitions == ["QUEUED", "RUNNING", "COMPLETED"]
    assert report.job.result.summary.total_findings == 6
    assert len(report.outcomes) == 1


def test_clean_pr_completes_with_no_findings():
    report = run_mock_execution(MockScenario.CLEAN)
    assert report.final_status is JobStatus.COMPLETED and report.job.result.findings == []


def test_flaky_recovers_within_attempts():
    report = run_mock_execution(MockScenario.FLAKY, flaky_failures=2, max_attempts=3)
    assert report.final_status is JobStatus.COMPLETED
    assert report.transitions == [
        "QUEUED", "RUNNING", "QUEUED", "RUNNING", "QUEUED", "RUNNING", "COMPLETED",
    ]
    assert report.job.attempts == 3 and report.job.error_message is None


def test_flaky_that_outlasts_the_attempts_fails():
    report = run_mock_execution(MockScenario.FLAKY, flaky_failures=5, max_attempts=3)
    assert report.final_status is JobStatus.FAILED
    assert len(report.outcomes) == 3 and report.job.error_message


@pytest.mark.parametrize(
    ("scenario", "attempts_used"),
    [(MockScenario.FAIL_PERMANENT, 1), (MockScenario.TIMEOUT, 3), (MockScenario.FAIL_RETRYABLE, 3)],
)
def test_failure_scenarios(scenario, attempts_used):
    report = run_mock_execution(scenario, max_attempts=3)
    assert report.final_status is JobStatus.FAILED
    assert len(report.outcomes) == attempts_used
    assert report.job.completed_at is not None


def test_custom_request_is_used():
    request = ReviewRequest(job_id="mine", repository="a/b", pr_number=9, commit_sha="dead")
    report = run_mock_execution(request=request)
    assert report.job.job_id == "mine" and report.job.result.pr_number == 9


def test_demo_cli_output_and_exit_codes(capsys):
    assert demo_main([]) == 0
    out = capsys.readouterr().out
    assert "status path: QUEUED -> RUNNING -> COMPLETED" in out and "6 finding(s)" in out

    assert demo_main(["--scenario", "flaky"]) == 0
    assert "retry in 2s" in capsys.readouterr().out

    assert demo_main(["--scenario", "fail_permanent"]) == 1
    assert "final status: FAILED" in capsys.readouterr().out


def test_demo_cli_rejects_unknown_scenario():
    with pytest.raises(SystemExit):
        demo_main(["--scenario", "chaos"])
