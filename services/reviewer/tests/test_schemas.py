from datetime import UTC, datetime, timedelta

import pytest
from app.schemas import (
    SCHEMA_VERSION,
    Category,
    ErrorCode,
    Finding,
    ReviewAgent,
    ReviewerError,
    ReviewerInfo,
    ReviewerMode,
    ReviewRequest,
    ReviewResult,
    ReviewSummary,
    Severity,
)
from pydantic import ValidationError

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def make_finding(**overrides) -> Finding:
    data = {
        "agent": ReviewAgent.GENERAL,
        "severity": Severity.HIGH,
        "category": Category.BUG,
        "file": "foo.py",
        "line": 42,
        "message": "something is wrong",
    }
    data.update(overrides)
    return Finding(**data)


def make_result(findings, **overrides) -> ReviewResult:
    request = ReviewRequest(job_id="j", repository="o/r", pr_number=1, commit_sha="abc")
    data = {
        "request": request,
        "reviewer": ReviewerInfo(name="t", version="1", mode=ReviewerMode.MOCK),
        "agents_run": [ReviewAgent.GENERAL],
        "findings": findings,
        "started_at": T0,
        "completed_at": T0 + timedelta(milliseconds=1500),
    }
    data.update(overrides)
    return ReviewResult.create(**data)


# ------------------------------- Finding ---------------------------------- #
def test_plan_example_finding_is_valid():
    """The exact example from the project plan (HIGH / BUG / foo.py / 42)."""
    f = make_finding(message="...")
    assert (f.severity, f.category, f.file, f.line) == ("HIGH", "BUG", "foo.py", 42)


def test_finding_gets_unique_default_ids():
    assert make_finding().id != make_finding().id


def test_vocabularies_match_the_plan():
    assert [s.value for s in Severity] == ["HIGH", "MEDIUM", "LOW"]
    assert {c.value for c in Category} == {
        "BUG", "SECURITY", "ARCHITECTURE", "MAINTAINABILITY", "STYLE",
    }
    assert {a.value for a in ReviewAgent} == {
        "STATIC_ANALYSIS", "SECURITY", "ARCHITECTURE", "CODE_STYLE", "GENERAL",
    }


def test_severity_rank_orders_high_first():
    assert Severity.HIGH.rank > Severity.MEDIUM.rank > Severity.LOW.rank


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("./src/a.py", "src/a.py"), ("  src/a.py ", "src/a.py"), ("a.py", "a.py")],
)
def test_file_is_normalised(raw, expected):
    assert make_finding(file=raw).file == expected


@pytest.mark.parametrize("bad", ["/etc/passwd", "../x.py", "a/../b.py", "a\\b.py", "  ", "./"])
def test_bad_file_paths_rejected(bad):
    with pytest.raises(ValidationError):
        make_finding(file=bad)


def test_pr_level_finding_without_file_or_line_is_valid():
    f = make_finding(file=None, line=None)
    assert f.file is None and f.line is None


def test_line_requires_file():
    with pytest.raises(ValidationError, match="line requires file"):
        make_finding(file=None, line=3)


def test_end_line_requires_line_and_must_not_precede_it():
    with pytest.raises(ValidationError, match="end_line requires line"):
        make_finding(line=None, end_line=5)
    with pytest.raises(ValidationError, match="end_line must be >= line"):
        make_finding(line=10, end_line=9)
    assert make_finding(line=10, end_line=10).end_line == 10


@pytest.mark.parametrize("line", [0, -1])
def test_line_must_be_positive(line):
    with pytest.raises(ValidationError):
        make_finding(line=line)


@pytest.mark.parametrize("confidence", [-0.1, 1.1])
def test_confidence_bounded(confidence):
    with pytest.raises(ValidationError):
        make_finding(confidence=confidence)


def test_message_is_stripped_and_cannot_be_blank():
    assert make_finding(message="  hi  ").message == "hi"
    with pytest.raises(ValidationError):
        make_finding(message="   ")
    with pytest.raises(ValidationError):
        make_finding(message="")


def test_blank_suggestion_becomes_none():
    assert make_finding(suggestion="   ").suggestion is None


def test_unknown_fields_rejected():
    with pytest.raises(ValidationError):
        make_finding(severty="HIGH")


def test_invalid_enum_values_rejected():
    with pytest.raises(ValidationError):
        make_finding(severity="CRITICAL")
    with pytest.raises(ValidationError):
        make_finding(category="PERFORMANCE")


# ------------------------------- Summary ---------------------------------- #
def test_summary_counts_and_zero_filled_keys():
    summary = ReviewSummary.from_findings(
        [make_finding(), make_finding(severity=Severity.LOW, category=Category.STYLE)]
    )
    assert summary.total_findings == 2
    assert summary.by_severity == {Severity.HIGH: 1, Severity.MEDIUM: 0, Severity.LOW: 1}
    assert summary.by_category[Category.SECURITY] == 0
    assert set(summary.by_category) == set(Category)
    assert set(summary.by_agent) == set(ReviewAgent)
    assert summary.highest_severity is Severity.HIGH


def test_empty_summary():
    summary = ReviewSummary.from_findings([])
    assert summary.total_findings == 0
    assert summary.highest_severity is None
    assert all(v == 0 for v in summary.by_severity.values())


# ------------------------------- Request ---------------------------------- #
def test_request_defaults_and_version():
    r = ReviewRequest(job_id="j", repository="owner/repo", pr_number=1, commit_sha="abc123")
    assert r.schema_version == SCHEMA_VERSION == "1.0"
    assert r.diff is None


@pytest.mark.parametrize("repo", ["noslash", "a/b/c", "/repo", "owner/", "ow ner/repo", ""])
def test_request_rejects_bad_repository(repo):
    with pytest.raises(ValidationError):
        ReviewRequest(job_id="j", repository=repo, pr_number=1, commit_sha="abc")


@pytest.mark.parametrize("pr", [0, -3])
def test_request_rejects_non_positive_pr(pr):
    with pytest.raises(ValidationError):
        ReviewRequest(job_id="j", repository="o/r", pr_number=pr, commit_sha="abc")


def test_request_rejects_empty_ids_and_unknown_fields():
    with pytest.raises(ValidationError):
        ReviewRequest(job_id="", repository="o/r", pr_number=1, commit_sha="abc")
    with pytest.raises(ValidationError):
        ReviewRequest(job_id="j", repository="o/r", pr_number=1, commit_sha="")
    with pytest.raises(ValidationError):
        ReviewRequest(job_id="j", repository="o/r", pr_number=1, commit_sha="abc", extra=1)


def test_request_rejects_unsupported_schema_version():
    with pytest.raises(ValidationError):
        ReviewRequest(
            schema_version="2.0", job_id="j", repository="o/r", pr_number=1, commit_sha="a"
        )


# ------------------------------- Result ----------------------------------- #
def test_create_sorts_findings_by_priority():
    low = make_finding(severity=Severity.LOW, file="a.py", line=1)
    high_b = make_finding(severity=Severity.HIGH, file="b.py", line=5)
    high_a = make_finding(severity=Severity.HIGH, file="a.py", line=9)
    pr_level = make_finding(severity=Severity.HIGH, file=None, line=None)
    mid = make_finding(severity=Severity.MEDIUM, file="z.py", line=1)

    result = make_result([low, pr_level, high_b, mid, high_a])

    assert result.findings == [high_a, high_b, pr_level, mid, low]


def test_create_computes_summary_and_duration():
    result = make_result([make_finding()])
    assert result.summary.total_findings == 1
    assert result.duration_ms == 1500
    assert result.job_id == "j" and result.pr_number == 1


def test_result_round_trips_through_json():
    result = make_result([make_finding(), make_finding(severity=Severity.LOW)])
    again = ReviewResult.model_validate_json(result.model_dump_json())
    assert again == result


def test_result_rejects_tampered_summary():
    payload = make_result([make_finding()]).model_dump(mode="json")
    payload["summary"]["total_findings"] = 99
    with pytest.raises(ValidationError, match="summary does not match findings"):
        ReviewResult.model_validate(payload)


def test_result_rejects_unsorted_findings():
    payload = make_result(
        [make_finding(severity=Severity.HIGH), make_finding(severity=Severity.LOW)]
    ).model_dump(mode="json")
    payload["findings"].reverse()
    with pytest.raises(ValidationError, match="sorted by priority"):
        ReviewResult.model_validate(payload)


def test_result_requires_timezone_aware_timestamps():
    with pytest.raises(ValidationError, match="timezone-aware"):
        make_result([], started_at=datetime(2026, 1, 1))


def test_result_rejects_completion_before_start():
    with pytest.raises(ValidationError, match="must not precede"):
        make_result([], completed_at=T0 - timedelta(seconds=1))


def test_empty_result_is_valid():
    result = make_result([])
    assert result.findings == [] and result.summary.highest_severity is None


# ------------------------------- Errors ----------------------------------- #
@pytest.mark.parametrize(
    ("code", "status", "retryable"),
    [
        (ErrorCode.INVALID_REQUEST, 422, False),
        (ErrorCode.UPSTREAM_ERROR, 502, True),
        (ErrorCode.REVIEW_TIMEOUT, 504, True),
        (ErrorCode.REVIEWER_UNAVAILABLE, 503, True),
        (ErrorCode.INTERNAL_ERROR, 500, False),
        (ErrorCode.INVALID_RESPONSE, 502, False),
    ],
)
def test_error_code_defaults(code, status, retryable):
    err = ReviewerError(code, "boom")
    assert (err.http_status, err.retryable) == (status, retryable)
    assert err.to_response().error.code is code


def test_retryable_can_be_overridden():
    assert ReviewerError(ErrorCode.UPSTREAM_ERROR, "perm", retryable=False).retryable is False
