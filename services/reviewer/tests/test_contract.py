"""The documented contract (docs/contracts) must match the code, byte for byte."""

import json
import re
from pathlib import Path

import pytest
from app import contract_export
from app.main import app, get_reviewer
from app.reviewers import MockReviewer, MockScenario
from app.schemas import (
    Category,
    ErrorCode,
    ErrorResponse,
    ReviewAgent,
    ReviewerMode,
    ReviewRequest,
    ReviewResult,
    Severity,
)
from fastapi.testclient import TestClient

CONTRACT_DIR = contract_export.CONTRACT_DIR
DOC = (CONTRACT_DIR / "reviewer-contract.md").read_text(encoding="utf-8")


def load(relative: str) -> dict:
    return json.loads((CONTRACT_DIR / relative).read_text(encoding="utf-8"))


# ------------------------- artifacts are up to date ------------------------ #
def test_committed_artifacts_match_the_code():
    stale = contract_export.stale_artifacts()
    assert stale == [], (
        f"Stale contract files {stale}. Run `python -m app.contract_export` "
        "in services/reviewer and commit the result."
    )


def test_every_artifact_exists_on_disk():
    for relative in contract_export.build_artifacts():
        assert (CONTRACT_DIR / relative).is_file(), relative


def test_export_writes_and_detects_staleness(tmp_path: Path):
    assert len(contract_export.stale_artifacts(tmp_path)) == len(contract_export.build_artifacts())
    contract_export.write_artifacts(tmp_path)
    assert contract_export.stale_artifacts(tmp_path) == []
    (tmp_path / "examples" / "review-request.json").write_text("{}")
    assert contract_export.stale_artifacts(tmp_path) == ["examples/review-request.json"]


def test_export_cli(capsys):
    assert contract_export.main(["--check"]) == 0
    assert "up to date" in capsys.readouterr().out
    assert contract_export.main(["--bogus"]) == 2


def test_export_is_deterministic():
    assert contract_export.build_artifacts() == contract_export.build_artifacts()


# --------------------------- examples are valid ---------------------------- #
@pytest.mark.parametrize("name", ["review-request", "review-request-with-diff"])
def test_request_examples_validate(name):
    ReviewRequest.model_validate(load(f"examples/{name}.json"))


@pytest.mark.parametrize("name", ["multi-agent", "single-bug", "clean"])
def test_result_examples_validate(name):
    ReviewResult.model_validate(load(f"examples/review-result-{name}.json"))


@pytest.mark.parametrize("name", ["invalid-request", "upstream-error"])
def test_error_examples_validate(name):
    ErrorResponse.model_validate(load(f"examples/error-{name}.json"))


def test_example_requests_and_results_agree():
    request = ReviewRequest.model_validate(load("examples/review-request.json"))
    for name in ("multi-agent", "single-bug", "clean"):
        result = ReviewResult.model_validate(load(f"examples/review-result-{name}.json"))
        assert (result.job_id, result.repository, result.pr_number, result.commit_sha) == (
            request.job_id, request.repository, request.pr_number, request.commit_sha,
        )


def test_golden_example_matches_a_live_review_over_http():
    """POSTing the example request returns the documented findings/summary."""
    app.dependency_overrides[get_reviewer] = lambda: MockReviewer(MockScenario.MULTI_AGENT)
    try:
        with TestClient(app) as client:
            live = client.post("/review", json=load("examples/review-request.json")).json()
    finally:
        app.dependency_overrides.clear()
    golden = load("examples/review-result-multi-agent.json")
    assert live["findings"] == golden["findings"]
    assert live["summary"] == golden["summary"]
    assert live["agents_run"] == golden["agents_run"]


# ------------------------------ JSON schemas ------------------------------- #
@pytest.mark.parametrize("name", ["review-request", "review-result", "error-response"])
def test_schemas_forbid_additional_properties_everywhere(name):
    schema = load(f"schemas/{name}.schema.json")
    objects = [schema, *schema.get("$defs", {}).values()]
    for obj in objects:
        if obj.get("type") == "object" and "properties" in obj:
            assert obj.get("additionalProperties") is False, obj.get("title")


def test_result_schema_lists_the_documented_enums():
    defs = load("schemas/review-result.schema.json")["$defs"]
    assert defs["Severity"]["enum"] == [s.value for s in Severity]
    assert defs["Category"]["enum"] == [c.value for c in Category]
    assert defs["ReviewAgent"]["enum"] == [a.value for a in ReviewAgent]


# ------------------------------ the prose doc ------------------------------ #
@pytest.mark.parametrize(
    "value",
    [
        *(e.value for e in Severity),
        *(e.value for e in Category),
        *(e.value for e in ReviewAgent),
        *(e.value for e in ReviewerMode),
        *(e.value for e in ErrorCode),
        *(e.value for e in MockScenario),
    ],
)
def test_doc_mentions_every_enum_value(value):
    assert f"`{value}`" in DOC, f"reviewer-contract.md does not document `{value}`"


def test_doc_error_table_matches_http_status_and_retryability():
    from app.schemas.errors import DEFAULT_RETRYABLE, HTTP_STATUS_BY_CODE

    for code in ErrorCode:
        if code is ErrorCode.INVALID_RESPONSE:
            continue  # client-side only, documented without an HTTP status
        row = next(line for line in DOC.splitlines() if line.startswith(f"| `{code.value}`"))
        cells = [c.strip() for c in row.strip("|").split("|")]
        assert cells[1] == str(HTTP_STATUS_BY_CODE[code]), row
        assert ("yes" in cells[2]) == DEFAULT_RETRYABLE[code], row


def test_doc_request_example_is_a_valid_request():
    section = DOC.split("## 3. Request")[1].split("## 4.")[0]
    block = re.search(r"```json\n(.*?)```", section, re.DOTALL).group(1)
    ReviewRequest.model_validate_json(block)


def test_doc_documents_all_endpoints():
    for endpoint in ("POST /review", "GET /healthz", "GET /info"):
        assert f"`{endpoint}`" in DOC
