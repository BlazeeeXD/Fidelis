"""Pydantic request/response schemas for the Orchestrator API."""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from app.models.job import JobStatus


class JobCreateRequest(BaseModel):
    repository: str = Field(..., pattern=r"^[\w.-]+/[\w.-]+$", examples=["owner/repo"])
    pr_number: int = Field(..., gt=0)
    commit_sha: str = Field(..., min_length=1, max_length=64)


class JobCreateResponse(BaseModel):
    job_id: str
    status: JobStatus
    duplicate: bool = False  # True if an existing job was returned


class JobDetailResponse(BaseModel):
    job_id: str
    repository: str
    pr_number: int
    commit_sha: str
    status: JobStatus
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error_message: Optional[str] = None