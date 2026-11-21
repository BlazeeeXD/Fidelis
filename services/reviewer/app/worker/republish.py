"""Republish a persisted review result without rerunning the LLM pipeline.

Usage inside the review-worker container:
  python -m app.worker.republish --job-id <completed-job-id>
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

import httpx

from app.github_reviews import GitHubReviewPublisher
from app.schemas import ReviewRequest, ReviewResult


async def _republish(job_id: str) -> dict:
    orchestrator = os.getenv("ORCHESTRATOR_URL", "http://orchestrator:8000").rstrip("/")
    worker_token = os.getenv("WORKER_STATUS_TOKEN", "")
    github_token = os.getenv("GITHUB_TOKEN", "")
    github_api = os.getenv("GITHUB_API_URL", "https://api.github.com")
    if not github_token:
        raise RuntimeError("GITHUB_TOKEN is not configured in review-worker")
    if not worker_token:
        raise RuntimeError("WORKER_STATUS_TOKEN is not configured in review-worker")

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(f"{orchestrator}/jobs/{job_id}/result")
        if response.status_code == 404:
            raise RuntimeError(f"No persisted result exists for job {job_id}")
        response.raise_for_status()
        result = ReviewResult.model_validate(response.json())

        request = ReviewRequest(
            job_id=result.job_id,
            repository=result.repository,
            pr_number=result.pr_number,
            commit_sha=result.commit_sha,
        )
        delivery = await GitHubReviewPublisher(github_token, api_url=github_api).publish(request, result)
        await client.post(
            f"{orchestrator}/jobs/{job_id}/delivery",
            headers={"X-Worker-Token": worker_token},
            json={
                "status": "published" if delivery.get("published") else "skipped",
                "message": str(delivery.get("reason", "republished persisted result"))[:500],
                "metadata": {
                    "action": delivery.get("action"),
                    "method": delivery.get("method"),
                    "comment_id": delivery.get("comment_id"),
                    "html_url": delivery.get("html_url"),
                    "comment_url": delivery.get("comment_url"),
                    "inline_comments": delivery.get("inline_comments", 0),
                    "republished": True,
                },
            },
        ).raise_for_status()
        return {"job_id": job_id, **delivery, "republished_without_llm": True}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args()
    try:
        result = asyncio.run(_republish(args.job_id))
    except Exception as exc:
        print(json.dumps({"published": False, "error": str(exc)[:1000]}, indent=2))
        return 1
    print(json.dumps(result, indent=2))
    return 0 if result.get("published") else 1


if __name__ == "__main__":
    raise SystemExit(main())
