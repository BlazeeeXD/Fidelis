"""Worker-to-Orchestrator job status event contract."""
from enum import StrEnum
from typing import Self, Any
from pydantic import BaseModel, ConfigDict, Field, model_validator


class JobStatusEvent(StrEnum):
    STARTED = "started"
    COMPLETED = "completed"
    RETRY_SCHEDULED = "retry_scheduled"
    FAILED = "failed"


class JobStatusUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: JobStatusEvent
    attempt: int = Field(default=1, ge=1)
    error_message: str | None = Field(default=None, max_length=500)
    retry_after_seconds: float | None = Field(default=None, ge=0)
    result: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_event_fields(self) -> Self:
        if self.event in {JobStatusEvent.RETRY_SCHEDULED, JobStatusEvent.FAILED}:
            if not self.error_message:
                raise ValueError(f"{self.event.value} requires error_message")
        elif self.error_message is not None:
            raise ValueError(f"{self.event.value} does not accept error_message")
        if self.event is JobStatusEvent.RETRY_SCHEDULED:
            if self.retry_after_seconds is None:
                raise ValueError("retry_scheduled requires retry_after_seconds")
        elif self.retry_after_seconds is not None:
            raise ValueError(f"{self.event.value} does not accept retry_after_seconds")
        if self.event is JobStatusEvent.COMPLETED and self.result is None:
            raise ValueError("completed requires the structured review result")
        if self.result is not None and self.event is not JobStatusEvent.COMPLETED:
            raise ValueError("result is only accepted with completed")
        return self
