"""Reference implementations of :class:`ReviewerClient`."""

import asyncio
from typing import Self

import httpx
from pydantic import ValidationError

from app.reviewers.base import Reviewer
from app.schemas import (
    ErrorCode,
    ErrorResponse,
    ReviewerError,
    ReviewRequest,
    ReviewResult,
)
from app.worker.interface import ReviewerClient

# Unparseable error bodies (e.g. from a proxy) are classified by status alone.
_RETRYABLE_STATUSES = frozenset({408, 429})


class InProcessReviewerClient(ReviewerClient):
    """Calls a :class:`Reviewer` object directly — no HTTP.

    For tests and the mock execution. It runs the async reviewer on its own
    event loop, so call it from synchronous code (like a Celery task), not from
    inside a running loop.
    """

    def __init__(self, reviewer: Reviewer) -> None:
        self._reviewer = reviewer

    def review(self, request: ReviewRequest) -> ReviewResult:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._reviewer.review(request))
        raise RuntimeError(
            "InProcessReviewerClient.review() is synchronous; call it from sync code, "
            "or await reviewer.review() directly inside an event loop."
        )


class HttpReviewerClient(ReviewerClient):
    """Calls a running Reviewer Service over HTTP (``POST /review``).

    Every failure is translated to a :class:`ReviewerError` following the rules
    in ``docs/contracts/reviewer-contract.md``:

    * transport failure or timeout            -> ``REVIEWER_UNAVAILABLE`` (retryable)
    * non-2xx with an ``ErrorResponse`` body  -> that error, verbatim
    * non-2xx with any other body             -> 408/429/5xx retryable
      ``REVIEWER_UNAVAILABLE``; other 4xx non-retryable ``INVALID_REQUEST``
    * 2xx that does not parse as a result     -> ``INVALID_RESPONSE`` (not retryable)
    """

    def __init__(
        self,
        base_url: str = "http://reviewer:8000",
        *,
        timeout: float = 120.0,
        client: httpx.Client | None = None,
    ) -> None:
        # An injected client (e.g. a Starlette TestClient in tests) must already
        # have its own base_url; ``base_url`` and ``timeout`` then do not apply.
        self._owns_client = client is None
        self._client = client or httpx.Client(base_url=base_url, timeout=timeout)

    def review(self, request: ReviewRequest) -> ReviewResult:
        try:
            response = self._client.post(
                "/review", json=request.model_dump(mode="json", exclude_none=True)
            )
        except httpx.HTTPError as exc:  # timeouts, connection errors, ...
            raise ReviewerError(
                ErrorCode.REVIEWER_UNAVAILABLE,
                f"Could not reach the reviewer service: {type(exc).__name__}: {exc}",
            ) from exc

        if response.is_success:
            return self._parse_result(response)
        raise self._to_error(response)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_result(response: httpx.Response) -> ReviewResult:
        try:
            return ReviewResult.model_validate_json(response.content)
        except ValidationError as exc:
            raise ReviewerError(
                ErrorCode.INVALID_RESPONSE,
                "Reviewer returned a 200 response that does not match the contract.",
                details={"errors": [
                    {"loc": [str(p) for p in e["loc"]], "msg": e["msg"], "type": e["type"]}
                    for e in exc.errors()
                ]},
            ) from exc

    @staticmethod
    def _to_error(response: httpx.Response) -> ReviewerError:
        try:
            body = ErrorResponse.model_validate_json(response.content)
        except ValidationError:
            status = response.status_code
            if status in _RETRYABLE_STATUSES or status >= 500:
                return ReviewerError(
                    ErrorCode.REVIEWER_UNAVAILABLE,
                    f"Reviewer returned HTTP {status} without a contract error body.",
                )
            return ReviewerError(
                ErrorCode.INVALID_REQUEST,
                f"Reviewer rejected the request with HTTP {status}.",
            )
        error = body.error
        return ReviewerError(
            error.code, error.message, retryable=error.retryable, details=error.details
        )
