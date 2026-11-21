import logging
import re
from pathlib import Path

import pytest
from app.schemas import ErrorCode, ErrorDetail
from app.worker import (
    ALLOWED_TRANSITIONS,
    InMemoryStatusReporter,
    InvalidTransitionError,
    JobStatus,
    LoggingStatusReporter,
    UnknownJobError,
)

from tests.conftest import FakeClock
from tests.test_schemas import make_result

ERR = ErrorDetail(code=ErrorCode.UPSTREAM_ERROR, message="flaked", retryable=True)
RESULT = make_result([])


@pytest.fixture
def rep() -> InMemoryStatusReporter:
    r = InMemoryStatusReporter(clock=FakeClock())
    r.add_job("j")
    return r


def test_new_jobs_start_queued(rep):
    assert rep.status_of("j") is JobStatus.QUEUED
    assert rep.jobs["j"].started_at is None


def test_duplicate_job_registration_is_an_error(rep):
    with pytest.raises(ValueError, match="already exists"):
        rep.add_job("j")


def test_unknown_job_raises(rep):
    for call in (
        lambda: rep.job_started("nope", attempt=1),
        lambda: rep.job_completed("nope", RESULT),
        lambda: rep.job_failed("nope", attempt=1, error=ERR),
        lambda: rep.status_of("nope"),
    ):
        with pytest.raises(UnknownJobError):
            call()


def test_terminal_states_have_no_exits():
    assert ALLOWED_TRANSITIONS[JobStatus.COMPLETED] == frozenset()
    assert ALLOWED_TRANSITIONS[JobStatus.FAILED] == frozenset()
    assert set(ALLOWED_TRANSITIONS) == set(JobStatus)


def test_history_records_each_change(rep):
    rep.job_started("j", attempt=1)
    rep.job_retry_scheduled("j", attempt=1, error=ERR, retry_after_seconds=2.0)
    rep.job_started("j", attempt=2)
    rep.job_completed("j", RESULT)

    assert [(c.from_status.value, c.to_status.value) for c in rep.history] == [
        ("QUEUED", "RUNNING"), ("RUNNING", "QUEUED"), ("QUEUED", "RUNNING"), ("RUNNING", "COMPLETED"),
    ]
    retry = rep.history[1]
    assert retry.attempt == 1 and retry.error == ERR and retry.retry_after_seconds == 2.0
    assert [c.at for c in rep.history] == sorted(c.at for c in rep.history)


@pytest.mark.parametrize(
    "action",
    [
        # Illegal from QUEUED (never started):
        lambda r: r.job_completed("j", RESULT),
        lambda r: r.job_failed("j", attempt=1, error=ERR),
        lambda r: r.job_retry_scheduled("j", attempt=1, error=ERR, retry_after_seconds=1),
    ],
)
def test_cannot_finish_or_retry_a_job_that_never_started(rep, action):
    with pytest.raises(InvalidTransitionError):
        action(rep)
    assert rep.status_of("j") is JobStatus.QUEUED and rep.history == []


@pytest.mark.parametrize("finish", ["complete", "fail"])
def test_terminal_jobs_reject_every_further_transition(rep, finish):
    rep.job_started("j", attempt=1)
    if finish == "complete":
        rep.job_completed("j", RESULT)
    else:
        rep.job_failed("j", attempt=1, error=ERR)
    final = rep.status_of("j")

    for action in (
        lambda: rep.job_started("j", attempt=2),
        lambda: rep.job_completed("j", RESULT),
        lambda: rep.job_failed("j", attempt=2, error=ERR),
        lambda: rep.job_retry_scheduled("j", attempt=2, error=ERR, retry_after_seconds=1),
    ):
        with pytest.raises(InvalidTransitionError):
            action()
    assert rep.status_of("j") is final


def test_failed_job_records_message_and_completion_time(rep):
    rep.job_started("j", attempt=1)
    rep.job_failed("j", attempt=1, error=ERR)
    job = rep.jobs["j"]
    assert job.error_message == "flaked" and job.completed_at is not None and job.result is None


def test_running_to_running_is_allowed_for_crash_recovery(rep):
    rep.job_started("j", attempt=1)
    first = rep.jobs["j"].started_at
    rep.job_started("j", attempt=1)
    assert rep.status_of("j") is JobStatus.RUNNING and rep.jobs["j"].started_at == first


def test_logging_reporter_logs_each_event(caplog):
    caplog.set_level(logging.INFO, logger="reviewer.worker")
    log = LoggingStatusReporter()
    log.job_started("j", attempt=1)
    log.job_retry_scheduled("j", attempt=1, error=ERR, retry_after_seconds=2)
    log.job_completed("j", RESULT)
    log.job_failed("j", attempt=3, error=ERR)
    text = caplog.text
    assert "RUNNING" in text and "retry in 2.0s" in text and "COMPLETED" in text and "FAILED" in text


# ---- JobStatus must stay identical to the orchestrator's / the contract ---- #
REPO_ROOT = Path(__file__).resolve().parents[3]


def test_job_status_matches_the_contract_doc():
    doc = (REPO_ROOT / "docs/contracts/review-job-contract.md").read_text(encoding="utf-8")
    line = next(ln for ln in doc.splitlines() if ln.startswith("QUEUED, RUNNING"))
    assert [s.strip() for s in line.split(",")] == [s.value for s in JobStatus]


def test_job_status_matches_the_orchestrator_model():
    source = REPO_ROOT / "services/orchestrator/app/models/job.py"
    if not source.is_file():
        pytest.skip("orchestrator source not present")
    block = source.read_text(encoding="utf-8").split("class JobStatus", 1)[1].split("@dataclass")[0]
    members = re.findall(r'^\s+(\w+)\s*=\s*"(\w+)"', block, re.MULTILINE)
    assert members == [(s.value, s.value) for s in JobStatus]
