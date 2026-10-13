"""Error model shared by the Reviewer Service and its callers.

Every non-2xx response from the reviewer has the body shape
:class:`ErrorResponse`. The important field is ``retryable``: it tells the
worker whether trying again could help (transient upstream/LLM/GitHub trouble)
or whether the job should be failed immediately (bad input, internal bug).
"""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ErrorCode(StrEnum):
    """Closed set of machine-readable error codes."""

    INVALID_REQUEST = "INVALID_REQUEST"  # caller sent something malformed (HTTP 422)
    UPSTREAM_ERROR = "UPSTREAM_ERROR"  # GitHub / LLM provider failed (HTTP 502)
    REVIEW_TIMEOUT = "REVIEW_TIMEOUT"  # the review took too long (HTTP 504)
    REVIEWER_UNAVAILABLE = "REVIEWER_UNAVAILABLE"  # overloaded / down (HTTP 503)
    INTERNAL_ERROR = "INTERNAL_ERROR"  # unexpected bug in the reviewer (HTTP 500)
    # Raised only by clients when a 2xx body does not match the contract.
    INVALID_RESPONSE = "INVALID_RESPONSE"


#: Default HTTP status for each error code.
HTTP_STATUS_BY_CODE: dict[ErrorCode, int] = {
    ErrorCode.INVALID_REQUEST: 422,
    ErrorCode.UPSTREAM_ERROR: 502,
    ErrorCode.REVIEW_TIMEOUT: 504,
    ErrorCode.REVIEWER_UNAVAILABLE: 503,
    ErrorCode.INTERNAL_ERROR: 500,
    ErrorCode.INVALID_RESPONSE: 502,
}

#: Whether each code is retryable unless the raiser says otherwise.
DEFAULT_RETRYABLE: dict[ErrorCode, bool] = {
    ErrorCode.INVALID_REQUEST: False,
    ErrorCode.UPSTREAM_ERROR: True,
    ErrorCode.REVIEW_TIMEOUT: True,
    ErrorCode.REVIEWER_UNAVAILABLE: True,
    ErrorCode.INTERNAL_ERROR: False,
    ErrorCode.INVALID_RESPONSE: False,
}


class ErrorDetail(BaseModel):
    """What went wrong and whether a retry is worthwhile."""

    model_config = ConfigDict(extra="forbid")

    code: ErrorCode
    message: str = Field(min_length=1)
    retryable: bool
    details: dict[str, Any] | None = Field(
        default=None, description="Optional structured context (e.g. validation errors)."
    )


class ErrorResponse(BaseModel):
    """Body of every non-2xx reviewer response."""

    model_config = ConfigDict(extra="forbid")

    error: ErrorDetail


class ReviewerError(Exception):
    """Raised by reviewers (and clients) to signal a controlled failure.

    ``retryable`` defaults from the error code but can be overridden per
    instance, e.g. a 4xx from GitHub that is known to be permanent.
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        retryable: bool | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = DEFAULT_RETRYABLE[code] if retryable is None else retryable
        self.details = details

    @property
    def http_status(self) -> int:
        return HTTP_STATUS_BY_CODE[self.code]

    def to_detail(self) -> ErrorDetail:
        return ErrorDetail(
            code=self.code,
            message=self.message,
            retryable=self.retryable,
            details=self.details,
        )

    def to_response(self) -> ErrorResponse:
        return ErrorResponse(error=self.to_detail())
