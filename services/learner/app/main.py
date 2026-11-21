"""HTTP API for starting and inspecting repository-learning jobs."""
import hmac
import os
import re

from celery.result import AsyncResult
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from app.celery_app import celery_app
from app.tasks import learn_repository

app = FastAPI(title="Fidelis Repository Learner", version="1.0.0")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class LearnRequest(BaseModel):
    max_prs: int = Field(default=30, ge=1, le=100)


def _auth(token: str | None) -> None:
    expected = os.getenv("WORKER_STATUS_TOKEN", "")
    if not expected or not token or not hmac.compare_digest(expected, token):
        raise HTTPException(status_code=401, detail="Invalid internal service token")


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/learn/{repository:path}", status_code=202)
def start_learning(repository: str, payload: LearnRequest, x_worker_token: str | None = Header(default=None, alias="X-Worker-Token")):
    _auth(x_worker_token)
    if not _REPOSITORY.fullmatch(repository):
        raise HTTPException(status_code=422, detail="Repository must be owner/repo")
    if not os.getenv("GITHUB_TOKEN"):
        raise HTTPException(status_code=503, detail="GITHUB_TOKEN is not configured")
    task = learn_repository.apply_async(args=[repository, payload.max_prs], queue="learning-jobs")
    return {"task_id": task.id, "repository": repository, "status": "PENDING"}


@app.get("/learn/{task_id}")
def learning_status(task_id: str, x_worker_token: str | None = Header(default=None, alias="X-Worker-Token")):
    _auth(x_worker_token)
    result = AsyncResult(task_id, app=celery_app)
    body = {"task_id": task_id, "status": result.state}
    if result.successful():
        body["result"] = result.result
    elif result.failed():
        body["error"] = str(result.result)[:500]
    return body
