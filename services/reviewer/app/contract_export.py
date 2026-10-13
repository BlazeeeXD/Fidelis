"""Generate the machine-readable contract artifacts under ``docs/contracts/``.

* ``schemas/*.schema.json`` — JSON Schema for the request, result and error body.
* ``examples/*.json``       — realistic, valid payloads other services can copy
  into their own tests and mocks.

Everything is derived from the Pydantic models and the mock reviewer, so the
documented contract cannot silently drift from the code. A test fails if the
committed files are stale.

Usage (from ``services/reviewer``)::

    python -m app.contract_export           # rewrite the files
    python -m app.contract_export --check   # exit 1 if they are out of date
"""

import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.reviewers import MockReviewer, MockScenario
from app.schemas import (
    ErrorCode,
    ErrorResponse,
    ReviewerError,
    ReviewRequest,
    ReviewResult,
)

# services/reviewer/app/contract_export.py -> repo root is parents[3]
CONTRACT_DIR = Path(__file__).resolve().parents[3] / "docs" / "contracts"

_EXAMPLE_JOB_ID = "5f0d3c1e-8a52-4a8e-9b8e-3c9a6d1f2b77"
_EXAMPLE_REPO = "acme/payments-api"
_EXAMPLE_PR = 42
_EXAMPLE_SHA = "9fceb02d0ae598e95dc970b74767f19372d61af8"
_EXAMPLE_DIFF = (
    "diff --git a/app/users.py b/app/users.py\n"
    "--- a/app/users.py\n"
    "+++ b/app/users.py\n"
    "@@ -10,3 +10,4 @@ def load_user(user_id):\n"
    "     user = db.get(user_id)\n"
    "+    return user.id\n"
    "     return user\n"
)


def _fixed_clock():
    """Clock that yields 12:00:00.000Z, then +250 ms, so examples never change."""
    ticks = iter(
        datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC) + timedelta(milliseconds=ms)
        for ms in (0, 250)
    )
    return lambda: next(ticks)


def _request(**overrides) -> ReviewRequest:
    data = {
        "job_id": _EXAMPLE_JOB_ID,
        "repository": _EXAMPLE_REPO,
        "pr_number": _EXAMPLE_PR,
        "commit_sha": _EXAMPLE_SHA,
    }
    data.update(overrides)
    return ReviewRequest(**data)


def _result(scenario: MockScenario, request: ReviewRequest) -> ReviewResult:
    reviewer = MockReviewer(scenario, clock=_fixed_clock())
    return asyncio.run(reviewer.review(request))


def _dump(model) -> str:
    return json.dumps(model.model_dump(mode="json"), indent=2) + "\n"


def _schema(model) -> str:
    return json.dumps(model.model_json_schema(), indent=2) + "\n"


def build_artifacts() -> dict[str, str]:
    """Return ``{path relative to docs/contracts: file content}``."""
    request = _request()
    request_with_diff = _request(diff=_EXAMPLE_DIFF)
    invalid = ReviewerError(
        ErrorCode.INVALID_REQUEST,
        "Request body does not match the review contract.",
        details={
            "errors": [
                {
                    "loc": ["body", "pr_number"],
                    "msg": "Input should be greater than 0",
                    "type": "greater_than",
                }
            ]
        },
    )
    upstream = ReviewerError(
        ErrorCode.UPSTREAM_ERROR, "GitHub API returned 502 while fetching the PR diff."
    )

    return {
        "schemas/review-request.schema.json": _schema(ReviewRequest),
        "schemas/review-result.schema.json": _schema(ReviewResult),
        "schemas/error-response.schema.json": _schema(ErrorResponse),
        "examples/review-request.json": _dump(request),
        "examples/review-request-with-diff.json": _dump(request_with_diff),
        "examples/review-result-multi-agent.json": _dump(
            _result(MockScenario.MULTI_AGENT, request)
        ),
        "examples/review-result-single-bug.json": _dump(
            _result(MockScenario.SINGLE_BUG, request)
        ),
        "examples/review-result-clean.json": _dump(_result(MockScenario.CLEAN, request)),
        "examples/error-invalid-request.json": _dump(invalid.to_response()),
        "examples/error-upstream-error.json": _dump(upstream.to_response()),
    }


def write_artifacts(directory: Path = CONTRACT_DIR) -> list[Path]:
    written = []
    for relative, content in build_artifacts().items():
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written


def stale_artifacts(directory: Path = CONTRACT_DIR) -> list[str]:
    """Relative paths whose on-disk content differs from the generated content."""
    stale = []
    for relative, content in build_artifacts().items():
        path = directory / relative
        if not path.is_file() or path.read_text(encoding="utf-8") != content:
            stale.append(relative)
    return stale


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args == ["--check"]:
        stale = stale_artifacts()
        if stale:
            print("Contract artifacts are out of date; run: python -m app.contract_export")
            for relative in stale:
                print(f"  - docs/contracts/{relative}")
            return 1
        print("Contract artifacts are up to date.")
        return 0
    if args:
        print("usage: python -m app.contract_export [--check]", file=sys.stderr)
        return 2
    for path in write_artifacts():
        print(f"wrote {path.relative_to(CONTRACT_DIR.parent.parent)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
