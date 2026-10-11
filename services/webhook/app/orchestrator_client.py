"""Client the Webhook Service uses to hand PR events to the Orchestrator.

Retrying is safe because POST /jobs is idempotent on
(repository, pr_number, commit_sha).

Timing budget: GitHub gives up on a webhook delivery after 10s. Worst case here
is 3 attempts x 2.5s + 0.25s + 0.5s of backoff = ~8.25s, so we still get to
answer GitHub (with a 502 it can redeliver) instead of silently timing out.
"""

import asyncio
import logging
import os
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger("webhook.orchestrator_client")


class OrchestratorError(Exception):
    """Orchestrator unreachable or failing (5xx)."""


class OrchestratorRejectedError(OrchestratorError):
    """Orchestrator returned 4xx: our payload is wrong. A bug, not an outage."""


class OrchestratorClient:
    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout: float = 2.5,
        max_retries: int = 2,
        backoff: float = 0.25,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.base_url = (
            base_url or os.getenv("ORCHESTRATOR_URL", "http://orchestrator:8000")
        ).rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff = backoff
        self._transport = transport  # tests inject a mock transport

    async def create_job(
        self, repository: str, pr_number: int, commit_sha: str
    ) -> Dict[str, Any]:
        payload = {
            "repository": repository,
            "pr_number": pr_number,
            "commit_sha": commit_sha,
        }
        last_error = "unknown error"

        async with httpx.AsyncClient(
            base_url=self.base_url, timeout=self.timeout, transport=self._transport
        ) as http:
            for attempt in range(self.max_retries + 1):
                try:
                    resp = await http.post("/jobs", json=payload)
                except httpx.TransportError as exc:
                    last_error = f"transport error: {exc!r}"
                else:
                    if resp.status_code < 300:
                        return resp.json()
                    if resp.status_code < 500:
                        raise OrchestratorRejectedError(
                            f"Orchestrator rejected job ({resp.status_code}): {resp.text}"
                        )
                    last_error = f"orchestrator returned {resp.status_code}"

                logger.warning(
                    "create_job attempt %d/%d failed: %s",
                    attempt + 1,
                    self.max_retries + 1,
                    last_error,
                )
                if attempt < self.max_retries:
                    await asyncio.sleep(self.backoff * (2**attempt))

        raise OrchestratorError(f"Could not create job: {last_error}")