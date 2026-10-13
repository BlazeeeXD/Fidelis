"""Reviewer Service (FastAPI).

Endpoints:

* ``GET  /healthz`` — liveness (used by docker compose).
* ``GET  /info``    — which reviewer implementation is active.
* ``POST /review``  — review one PR commit; body/response are defined in
  ``app/schemas`` and documented in ``docs/contracts/reviewer-contract.md``.

Today the active reviewer is the deterministic mock (``REVIEWER_MODE=mock``);
the real LangGraph reviewer replaces it in Week 4 behind the same endpoint.
"""

import logging
from functools import lru_cache
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.config import Settings
from app.reviewers import Reviewer
from app.reviewers.factory import create_reviewer
from app.schemas import (
    ErrorCode,
    ErrorDetail,
    ErrorResponse,
    ReviewerError,
    ReviewerInfo,
    ReviewRequest,
    ReviewResult,
)

logger = logging.getLogger("reviewer.main")

app = FastAPI(
    title="PR Reviewer - Reviewer Service",
    version="0.2.0",
    description="Reviews a pull request commit and returns structured findings.",
)


@lru_cache
def get_reviewer() -> Reviewer:
    """The process-wide reviewer. Tests replace it via ``dependency_overrides``."""
    return create_reviewer(Settings.from_env())


# --------------------------------------------------------------------------- #
# Error handling: every non-2xx body is an ErrorResponse.
# --------------------------------------------------------------------------- #
def _error_response(status_code: int, detail: ErrorDetail) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=ErrorResponse(error=detail).model_dump(mode="json"),
    )


@app.exception_handler(ReviewerError)
async def _handle_reviewer_error(_: Request, exc: ReviewerError) -> JSONResponse:
    logger.warning("review failed: code=%s retryable=%s msg=%s", exc.code, exc.retryable, exc.message)
    return _error_response(exc.http_status, exc.to_detail())


@app.exception_handler(RequestValidationError)
async def _handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    problems: list[dict[str, Any]] = [
        {"loc": [str(part) for part in err["loc"]], "msg": err["msg"], "type": err["type"]}
        for err in exc.errors()
    ]
    error = ReviewerError(
        ErrorCode.INVALID_REQUEST,
        "Request body does not match the review contract.",
        details={"errors": problems},
    )
    return _error_response(error.http_status, error.to_detail())


@app.exception_handler(Exception)
async def _handle_unexpected_error(_: Request, exc: Exception) -> JSONResponse:
    # Log the traceback server-side; never leak internals to the caller.
    logger.exception("unexpected error while handling request", exc_info=exc)
    error = ReviewerError(ErrorCode.INTERNAL_ERROR, "Internal reviewer error.")
    return _error_response(error.http_status, error.to_detail())


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/info", response_model=ReviewerInfo)
async def info(reviewer: Reviewer = Depends(get_reviewer)) -> ReviewerInfo:  # noqa: B008
    return reviewer.info()


_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    422: {"model": ErrorResponse, "description": "INVALID_REQUEST"},
    500: {"model": ErrorResponse, "description": "INTERNAL_ERROR"},
    502: {"model": ErrorResponse, "description": "UPSTREAM_ERROR (retryable)"},
    503: {"model": ErrorResponse, "description": "REVIEWER_UNAVAILABLE (retryable)"},
    504: {"model": ErrorResponse, "description": "REVIEW_TIMEOUT (retryable)"},
}


@app.post("/review", response_model=ReviewResult, responses=_ERROR_RESPONSES)
async def review(
    request: ReviewRequest,
    reviewer: Reviewer = Depends(get_reviewer),  # noqa: B008
) -> ReviewResult:
    logger.info(
        "review requested: job_id=%s repo=%s pr=%s sha=%s",
        request.job_id, request.repository, request.pr_number, request.commit_sha,
    )
    result = await reviewer.review(request)
    logger.info(
        "review done: job_id=%s findings=%s duration_ms=%s",
        result.job_id, result.summary.total_findings, result.duration_ms,
    )
    return result
