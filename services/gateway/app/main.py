"""
Gateway Service (FastAPI)

Responsibilities (per synopsis Section 3):
  - Single external entry point
  - Authentication (JWT) and authorization
  - Routing to internal services (Webhook, Orchestrator, Reviewer, ...)

Design note:
  GitHub webhooks are routed straight through to the Webhook Service
  WITHOUT a JWT check — GitHub itself doesn't send one, and it can't
  be configured to. Trust for that path comes from HMAC signature
  verification inside the Webhook Service itself. Every other route
  (dashboards, manual triggers, future admin APIs) goes through
  require_auth.

Run locally:
    uvicorn gateway.main:app --reload --port 8000
"""

import os
import httpx
from fastapi import FastAPI, Request, Depends, Response
from .auth import require_auth, create_access_token

WEBHOOK_SERVICE_URL = os.environ.get("WEBHOOK_SERVICE_URL", "http://localhost:8001")
ORCHESTRATOR_SERVICE_URL = os.environ.get("ORCHESTRATOR_SERVICE_URL", "http://localhost:8002")

app = FastAPI(title="PR Reviewer - Gateway Service")

_http_client = httpx.AsyncClient(timeout=15.0)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.post("/auth/dev-token")
async def dev_token(subject: str = "dev-user"):
    """
    DEV-ONLY convenience endpoint to mint a token for testing internal
    routes with curl/Postman. Remove or lock this down before any real
    deployment — issuing tokens with no credential check is only
    acceptable for local development.
    """
    return {"access_token": create_access_token(subject)}


@app.api_route("/webhook/{path:path}", methods=["POST", "GET"])
async def route_to_webhook_service(path: str, request: Request):
    """
    Unauthenticated passthrough to the Webhook Service. Security for
    this path comes from GitHub's HMAC signature, checked downstream.
    """
    return await _proxy(request, f"{WEBHOOK_SERVICE_URL}/webhook/{path}")


@app.api_route("/api/orchestrator/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def route_to_orchestrator(path: str, request: Request, _claims: dict = Depends(require_auth)):
    """
    Authenticated passthrough to the Orchestrator Service, e.g. for a
    dashboard to query job status or trigger a manual re-review.
    """
    return await _proxy(request, f"{ORCHESTRATOR_SERVICE_URL}/{path}")


async def _proxy(request: Request, target_url: str) -> Response:
    body = await request.body()
    upstream_response = await _http_client.request(
        method=request.method,
        url=target_url,
        headers={k: v for k, v in request.headers.items() if k.lower() != "host"},
        content=body,
        params=request.query_params,
    )
    return Response(
        content=upstream_response.content,
        status_code=upstream_response.status_code,
        headers={
            k: v for k, v in upstream_response.headers.items()
            if k.lower() not in ("content-length", "transfer-encoding", "connection")
        },
    )
