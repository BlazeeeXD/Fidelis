"""Background repository learning task."""
import logging
import os
from urllib.parse import quote

import httpx

from app.celery_app import celery_app
from app.extractor import GitHubLearningClient

logger = logging.getLogger("learner.tasks")


@celery_app.task(bind=True, name="fidelis.learn_repository", max_retries=5, acks_late=True, reject_on_worker_lost=True)
def learn_repository(self, repository: str, max_prs: int = 30) -> dict:
    token = os.getenv("GITHUB_TOKEN", "")
    worker_token = os.getenv("WORKER_STATUS_TOKEN", "")
    orchestrator_url = os.getenv("ORCHESTRATOR_URL", "http://orchestrator:8000").rstrip("/")
    api_url = os.getenv("GITHUB_API_URL", "https://api.github.com")
    try:
        result = GitHubLearningClient(token, api_url=api_url).mine(repository, max_prs=max_prs)
        if result["rules"]:
            response = httpx.put(
                f"{orchestrator_url}/repositories/{quote(repository, safe='/')}/rules",
                headers={"X-Worker-Token": worker_token},
                json={"rules": result["rules"]},
                timeout=15.0,
            )
            response.raise_for_status()
            result["stored_rule_count"] = len(response.json().get("rules", []))
        else:
            result["stored_rule_count"] = 0
        return result
    except Exception as exc:
        attempt = self.request.retries
        logger.exception("repository learning failed for %s", repository)
        if attempt >= self.max_retries:
            raise
        raise self.retry(exc=exc, countdown=min(60, 2 ** attempt)) from exc
