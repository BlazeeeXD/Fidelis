from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query, Response, status

from app.models.job import JobStatus, ReviewJob
from app.schemas.job import JobCreateRequest, JobCreateResponse, JobDetailResponse
from app.services.orchestrator import JobNotFoundError, orchestrator_service

router = APIRouter(prefix="/jobs", tags=["jobs"])


def _to_detail(job: ReviewJob) -> JobDetailResponse:
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


# Plain `def` (not `async def`): the service uses blocking SQLAlchemy sessions,
# so FastAPI runs these in its threadpool instead of blocking the event loop.
@router.post("", response_model=JobCreateResponse, status_code=status.HTTP_201_CREATED)
def create_job(payload: JobCreateRequest, response: Response) -> JobCreateResponse:
    job, created = orchestrator_service.create_or_get_job(
        repository=payload.repository,
        pr_number=payload.pr_number,
        commit_sha=payload.commit_sha,
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    return JobCreateResponse(job_id=job.id, status=job.status, duplicate=not created)


@router.get("", response_model=List[JobDetailResponse])
def list_jobs(
    repository: Optional[str] = None,
    pr_number: Optional[int] = None,
    status_filter: Optional[JobStatus] = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
) -> List[JobDetailResponse]:
    jobs = orchestrator_service.list_jobs(repository, pr_number, status_filter, limit)
    return [_to_detail(j) for j in jobs]


@router.get("/{job_id}", response_model=JobDetailResponse)
def get_job(job_id: str) -> JobDetailResponse:
    try:
        job = orchestrator_service.get_job(job_id)
    except JobNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found")
    return _to_detail(job)