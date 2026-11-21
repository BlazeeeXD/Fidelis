"""Repository-specific learned conventions API."""
import hmac
import os
import re

from fastapi import APIRouter, Header, HTTPException

from app.schemas.repository import RepositoryRulesUpdate
from app.services.orchestrator import orchestrator_service

router = APIRouter(prefix="/repositories", tags=["repository-learning"])
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _check_worker_token(token: str | None) -> None:
    expected = os.getenv("WORKER_STATUS_TOKEN", "")
    if not expected or not token or not hmac.compare_digest(expected, token):
        raise HTTPException(status_code=401, detail="Invalid internal service token")


@router.get("/{repository:path}/rules")
def get_rules(repository: str):
    if not _REPOSITORY.fullmatch(repository):
        raise HTTPException(status_code=422, detail="Repository must be owner/repo")
    return {"repository": repository, "rules": orchestrator_service.get_repository_rules(repository)}


@router.put("/{repository:path}/rules")
def update_rules(
    repository: str,
    payload: RepositoryRulesUpdate,
    x_worker_token: str | None = Header(default=None, alias="X-Worker-Token"),
):
    _check_worker_token(x_worker_token)
    if not _REPOSITORY.fullmatch(repository):
        raise HTTPException(status_code=422, detail="Repository must be owner/repo")
    try:
        rules = [item.model_dump() for item in payload.rules]
        stored = orchestrator_service.upsert_repository_rules(repository, rules)
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"repository": repository, "rules": stored, "updated": len(stored)}
