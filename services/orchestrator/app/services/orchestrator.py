"""In-memory job store and orchestration logic.

Week 1 uses a dict. Week 2+ swaps this for P4's Postgres-backed
repository without changing the API layer, since callers only depend
on this module's function signatures.
"""
from typing import Dict, Optional

from app.models.job import ReviewJob


class JobNotFoundError(Exception):
    pass


class OrchestratorService:
    def __init__(self) -> None:
        self._jobs: Dict[str, ReviewJob] = {}

    def create_job(self, repository: str, pr_number: int, commit_sha: str) -> ReviewJob:
        job = ReviewJob(repository=repository, pr_number=pr_number, commit_sha=commit_sha)
        self._jobs[job.id] = job
        return job

    def get_job(self, job_id: str) -> ReviewJob:
        job = self._jobs.get(job_id)
        if job is None:
            raise JobNotFoundError(job_id)
        return job


# Module-level singleton so the same store is shared across requests
# within this process. Fine for Week 1; goes away once P4's DB lands.
orchestrator_service = OrchestratorService()