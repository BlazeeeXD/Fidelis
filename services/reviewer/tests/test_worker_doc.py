"""The worker-interface doc must not drift from the code it describes."""

import re
from pathlib import Path

import pytest
from app.reviewers import MockScenario
from app.worker import (
    ALLOWED_TRANSITIONS,
    ExecutionDisposition,
    JobStatus,
    JobStatusReporter,
    ReviewWorker,
)
from app.worker.runner import backoff_seconds

DOCS = Path(__file__).resolve().parents[3] / "docs" / "contracts"
DOC_PATH = DOCS / "review-worker-interface.md"

if not DOC_PATH.exists():  # e.g. the service is tested from an image without docs/
    pytest.skip("contract docs not present", allow_module_level=True)

DOC = DOC_PATH.read_text(encoding="utf-8")


def test_every_status_and_disposition_is_documented():
    for status in JobStatus:
        assert f"`{status.value}`" in DOC
    for disposition in ExecutionDisposition:
        assert f"`{disposition.value}`" in DOC


def test_every_reporter_method_is_documented():
    methods = {
        name
        for name, value in vars(JobStatusReporter).items()
        if getattr(value, "__isabstractmethod__", False)
    }
    assert methods == {"job_started", "job_completed", "job_retry_scheduled", "job_failed"}
    for name in methods:
        assert f"`{name}(" in DOC


def test_documented_transitions_match_the_state_machine():
    # Only section 1 holds the transitions table; its rows look like:
    #   | `RUNNING` | `QUEUED` | ... |
    section = DOC.split("## 1. Job status state machine")[1].split("\n## 2.")[0]
    documented = {
        (match.group(1), match.group(2))
        for match in re.finditer(r"^\| `([A-Z]+)` \| `([A-Z]+)` \|", section, re.MULTILINE)
    }
    actual = {
        (source.value, target.value)
        for source, targets in ALLOWED_TRANSITIONS.items()
        for target in targets
    }
    assert documented == actual


def test_documented_retry_policy_matches_the_defaults():
    assert [backoff_seconds(n) for n in (1, 2, 3)] == [2.0, 4.0, 8.0]
    assert backoff_seconds(20) == 60.0
    assert "2 s, 4 s, 8 s" in DOC and "capped at 60 s" in DOC
    assert "max_attempts=3" in DOC
    assert ReviewWorker.__init__.__kwdefaults__["max_attempts"] == 3


@pytest.mark.parametrize("scenario", [s.value for s in MockScenario])
def test_scenarios_the_demo_accepts_are_the_documented_scenarios(scenario):
    contract = (DOCS / "reviewer-contract.md").read_text(encoding="utf-8")
    assert f"`{scenario}`" in contract


def test_relative_links_between_contract_docs_resolve():
    for doc in DOCS.glob("*.md"):
        text = doc.read_text(encoding="utf-8")
        for target in re.findall(r"\]\((?!https?://|#)([^)#]+)(?:#[^)]*)?\)", text):
            assert (doc.parent / target).exists(), f"{doc.name}: broken link {target}"
