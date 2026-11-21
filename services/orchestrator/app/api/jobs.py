from typing import Annotated
import hmac
import logging
import os

from fastapi import Header

from fastapi import APIRouter, HTTPException, Query, Response, status

from app.models.job import JobStatus, ReviewJob
from app.schemas.job import JobCreateRequest, JobCreateResponse, JobDetailResponse
from app.schemas.status import JobStatusUpdateRequest
from app.services.queue_dispatch import dispatch_pending_job
from app.services.orchestrator import InvalidJobTransitionError
from app.services.orchestrator import JobNotFoundError, orchestrator_service

logger = logging.getLogger("orchestrator.api.jobs")

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

    # Dispatch intent is committed with the job. A failed broker publish
    # leaves the outbox pending so the periodic sweeper can recover it.
    async_enabled = os.getenv("ASYNC_REVIEW_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
    if async_enabled and job.status.value == "QUEUED":
        try:
            dispatch_pending_job(job.id)
        except Exception as exc:
            logger.exception("review job %s is persisted but queue dispatch is pending", job.id)
            raise HTTPException(
                status_code=503,
                detail="Review job was persisted but could not be published; retry the webhook delivery.",
            ) from exc

    if not created:
        response.status_code = status.HTTP_200_OK
    return JobCreateResponse(job_id=job.id, status=job.status, duplicate=not created)


@router.get("", response_model=list[JobDetailResponse])
def list_jobs(
    repository: str | None = None,
    pr_number: int | None = None,
    status_filter: Annotated[JobStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[JobDetailResponse]:
    jobs = orchestrator_service.list_jobs(repository, pr_number, status_filter, limit)
    return [_to_detail(j) for j in jobs]


@router.get("/{job_id}", response_model=JobDetailResponse)
def get_job(job_id: str) -> JobDetailResponse:
    try:
        job = orchestrator_service.get_job(job_id)
    except JobNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found")
    return _to_detail(job)

@router.post("/{job_id}/status", response_model=JobDetailResponse, tags=["worker-internal"])
def update_job_status(
    job_id: str,
    payload: JobStatusUpdateRequest,
    x_worker_token: str | None = Header(default=None, alias="X-Worker-Token"),
) -> JobDetailResponse:
    """Authenticated lifecycle callback used by the Celery review worker."""
    expected = os.getenv("WORKER_STATUS_TOKEN", "")
    if not expected or not x_worker_token or not hmac.compare_digest(expected, x_worker_token):
        raise HTTPException(status_code=401, detail="Invalid worker token")
    try:
        job = orchestrator_service.update_job_status(
            job_id,
            payload.event.value,
            error_message=payload.error_message,
            result_payload=payload.result,
            attempt=payload.attempt,
            retry_after_seconds=payload.retry_after_seconds,
        )
    except JobNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found")
    except InvalidJobTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return _to_detail(job)


@router.get("/{job_id}/result")
def get_job_result(job_id: str):
    try:
        result = orchestrator_service.get_result(job_id)
    except JobNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found")
    if result is None:
        raise HTTPException(status_code=404, detail="Review result is not available yet")
    return result


@router.get("/{job_id}/events")
def get_job_events(job_id: str):
    try:
        return orchestrator_service.list_audit_events(job_id)
    except JobNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found")


@router.post("/{job_id}/replay", response_model=JobCreateResponse, status_code=status.HTTP_201_CREATED)
def replay_job(job_id: str, response: Response):
    try:
        job, created = orchestrator_service.create_replay_job(job_id)
    except JobNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found")
    async_enabled = os.getenv("ASYNC_REVIEW_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
    if async_enabled and job.status.value == "QUEUED" and created:
        try:
            dispatch_pending_job(job.id)
        except Exception as exc:
            logger.exception("replay job %s persisted but dispatch failed", job.id)
            raise HTTPException(status_code=503, detail="Replay was persisted but could not be dispatched") from exc
    if not created:
        response.status_code = status.HTTP_200_OK
    return JobCreateResponse(job_id=job.id, status=job.status, duplicate=not created)


def _require_worker_token(x_worker_token: str | None) -> None:
    expected = os.getenv("WORKER_STATUS_TOKEN", "")
    if not expected or not x_worker_token or not hmac.compare_digest(expected, x_worker_token):
        raise HTTPException(status_code=401, detail="Invalid worker token")


@router.get("/{job_id}/checkpoints", tags=["worker-internal"])
def get_agent_checkpoints(
    job_id: str,
    context_fingerprint: str,
    x_worker_token: str | None = Header(default=None, alias="X-Worker-Token"),
):
    """Restore completed agent outputs for an exact review-context fingerprint."""
    _require_worker_token(x_worker_token)
    if len(context_fingerprint) != 64 or any(ch not in "0123456789abcdef" for ch in context_fingerprint.lower()):
        raise HTTPException(status_code=422, detail="context_fingerprint must be a SHA-256 hex digest")
    try:
        return {"job_id": job_id, "checkpoints": orchestrator_service.list_agent_checkpoints(job_id, context_fingerprint)}
    except JobNotFoundError:
        raise HTTPException(status_code=404, detail="Review job not found")


@router.post("/{job_id}/checkpoints", tags=["worker-internal"])
def save_agent_checkpoint(
    job_id: str,
    payload: dict,
    x_worker_token: str | None = Header(default=None, alias="X-Worker-Token"),
):
    """Persist a completed agent pass so subsequent retries can skip it."""
    _require_worker_token(x_worker_token)
    agent = payload.get("agent")
    valid_agents = {"STATIC_ANALYSIS", "SECURITY", "ARCHITECTURE", "CODE_STYLE", "GENERAL"}
    fingerprint = payload.get("context_fingerprint")
    checkpoint = payload.get("payload")
    if agent not in valid_agents:
        raise HTTPException(status_code=422, detail="agent is not a supported reviewer agent")
    if not isinstance(fingerprint, str) or len(fingerprint) != 64 or any(ch not in "0123456789abcdef" for ch in fingerprint.lower()):
        raise HTTPException(status_code=422, detail="context_fingerprint must be a SHA-256 hex digest")
    if not isinstance(checkpoint, dict) or not isinstance(checkpoint.get("findings"), list) or not isinstance(checkpoint.get("agent_run"), dict):
        raise HTTPException(status_code=422, detail="payload must contain findings[] and agent_run object")
    if len(str(checkpoint).encode("utf-8")) > 1_000_000:
        raise HTTPException(status_code=413, detail="agent checkpoint exceeds 1 MB")
    if checkpoint["agent_run"].get("agent") != agent:
        raise HTTPException(status_code=422, detail="agent_run does not match checkpoint agent")
    try:
        orchestrator_service.save_agent_checkpoint(job_id, agent, fingerprint, checkpoint)
    except JobNotFoundError:
        raise HTTPException(status_code=404, detail="Review job not found")
    return {"job_id": job_id, "agent": agent, "saved": True}


@router.post("/{job_id}/delivery", tags=["worker-internal"])
def record_delivery_event(
    job_id: str,
    payload: dict,
    x_worker_token: str | None = Header(default=None, alias="X-Worker-Token"),
):
    """Record whether GitHub accepted the generated review."""
    expected = os.getenv("WORKER_STATUS_TOKEN", "")
    if not expected or not x_worker_token or not hmac.compare_digest(expected, x_worker_token):
        raise HTTPException(status_code=401, detail="Invalid worker token")
    status_value = payload.get("status")
    if status_value not in {"published", "failed", "skipped"}:
        raise HTTPException(status_code=422, detail="status must be published, failed, or skipped")
    message = str(payload.get("message", ""))[:1000]
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    try:
        orchestrator_service.add_delivery_event(job_id, status_value, message, metadata)
    except JobNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"job_id": job_id, "status": status_value, "recorded": True}
