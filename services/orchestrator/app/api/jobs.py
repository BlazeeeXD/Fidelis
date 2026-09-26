from fastapi import APIRouter, HTTPException, status

from app.schemas.job import JobCreateRequest, JobCreateResponse, JobDetailResponse
from app.services.orchestrator import JobNotFoundError, orchestrator_service

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.post("", response_model=JobCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_job(payload: JobCreateRequest) -> JobCreateResponse:
    job = orchestrator_service.create_job(
        repository=payload.repository,
        pr_number=payload.pr_number,
        commit_sha=payload.commit_sha,
    )
    return JobCreateResponse(job_id=job.id, status=job.status)


@router.get("/{job_id}", response_model=JobDetailResponse)
async def get_job(job_id: str) -> JobDetailResponse:
    try:
        job = orchestrator_service.get_job(job_id)
    except JobNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found")

    return JobDetailResponse(
        job_id=job.id,
        repository=job.repository,
        pr_number=job.pr_number,
        commit_sha=job.commit_sha,
        status=job.status,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        error_message=job.error_message,
    )