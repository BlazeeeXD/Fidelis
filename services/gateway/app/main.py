"""Fidelis API Gateway plus the Week 2 teacher-facing demo console.

The demo endpoints are intended for local development only. They surface
service health, list jobs through the authenticated orchestrator proxy, and
send a synthetic *but correctly HMAC-signed* pull_request event into the real
webhook handler. Demo-triggering endpoints return 404 in production.
"""

import asyncio
import hashlib
import hmac
import json
import os
import uuid
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse

from .auth import create_access_token, require_auth

WEBHOOK_SERVICE_URL = os.environ.get("WEBHOOK_SERVICE_URL", "http://localhost:8001")
ORCHESTRATOR_SERVICE_URL = os.environ.get(
    "ORCHESTRATOR_SERVICE_URL", "http://localhost:8002"
)
REVIEWER_SERVICE_URL = os.environ.get("REVIEWER_SERVICE_URL", "http://localhost:8003")
LEARNER_SERVICE_URL = os.environ.get("LEARNER_SERVICE_URL", "http://localhost:8004")
WORKER_STATUS_TOKEN = os.environ.get("WORKER_STATUS_TOKEN", "")
GITHUB_WEBHOOK_SECRET = os.environ.get(
    "GITHUB_WEBHOOK_SECRET", "dev_webhook_secret_change_me"
)
ENVIRONMENT = os.environ.get("ENV", "development").strip().lower()
APP_DIR = Path(__file__).resolve().parent
DASHBOARD_PATH = APP_DIR / "static" / "index.html"
DEMO_PAYLOAD_PATH = APP_DIR / "demo_payload.json"

app = FastAPI(title="Fidelis - Gateway Service")
_http_client = httpx.AsyncClient(timeout=15.0)
_last_demo_delivery: dict | None = None


@app.get("/", include_in_schema=False)
async def dashboard():
    """Serve the local Week 2 demo console."""
    return FileResponse(DASHBOARD_PATH, media_type="text/html")


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.post("/auth/dev-token")
async def dev_token(subject: str = "dev-user"):
    """DEV-ONLY convenience endpoint for the local dashboard and API tests."""
    if ENVIRONMENT in {"prod", "production"}:
        raise HTTPException(status_code=404, detail="Development token endpoint is disabled outside development")
    return {"access_token": create_access_token(subject)}


@app.get("/demo-api/health")
async def demo_health():
    """Probe the five HTTP services from inside the Compose network."""
    targets = [
        ("gateway", "Gateway", "http://127.0.0.1:8000/healthz", {"ok"}),
        ("webhook", "Webhook", f"{WEBHOOK_SERVICE_URL}/healthz", {"ok"}),
        ("orchestrator", "Orchestrator", f"{ORCHESTRATOR_SERVICE_URL}/health", {"healthy"}),
        ("reviewer", "Reviewer", f"{REVIEWER_SERVICE_URL}/healthz", {"ok"}),
        ("learner", "Learner", f"{LEARNER_SERVICE_URL}/healthz", {"ok"}),
    ]

    async def probe(key: str, name: str, url: str, expected: set[str]) -> dict:
        try:
            response = await _http_client.get(url, timeout=2.5)
            payload = response.json() if response.content else {}
            body_status = payload.get("status") if isinstance(payload, dict) else None
            healthy = response.status_code == 200 and body_status in expected
            return {
                "key": key,
                "name": name,
                "status": "healthy" if healthy else "unhealthy",
                "http_status": response.status_code,
                "detail": body_status or f"HTTP {response.status_code}",
            }
        except (httpx.HTTPError, ValueError) as exc:
            return {
                "key": key,
                "name": name,
                "status": "unreachable",
                "http_status": None,
                "detail": str(exc).splitlines()[0][:110],
            }

    services = await asyncio.gather(*(probe(*target) for target in targets))
    return {
        "services": services,
        "healthy_count": sum(service["status"] == "healthy" for service in services),
        "service_count": len(services),
        "scope_note": "HTTP health covers five services; PostgreSQL and Redis availability are covered by service healthchecks and integration tests.",
    }


@app.post("/demo-api/events")
async def send_demo_event(action: str = Query(default="opened")):
    """Inject a signed sample PR event into the actual Webhook service."""
    _require_development_demo()
    if action not in {"opened", "synchronize", "reopened"}:
        raise HTTPException(status_code=422, detail="Action must be opened, synchronize, or reopened")
    if not GITHUB_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="GITHUB_WEBHOOK_SECRET is not configured")

    global _last_demo_delivery
    payload = json.loads(DEMO_PAYLOAD_PATH.read_text(encoding="utf-8"))
    payload["action"] = action
    commit_sha = hashlib.sha1(uuid.uuid4().bytes).hexdigest()
    payload["pull_request"]["head"]["sha"] = commit_sha
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    delivery_id = str(uuid.uuid4())
    signature = "sha256=" + hmac.new(
        GITHUB_WEBHOOK_SECRET.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": delivery_id,
        "X-Hub-Signature-256": signature,
    }

    try:
        response = await _http_client.post(
            f"{WEBHOOK_SERVICE_URL}/webhook/github", content=body, headers=headers
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Webhook service unavailable: {exc}") from exc

    try:
        result = response.json()
    except ValueError:
        result = {"detail": response.text[:300]}
    if response.status_code >= 500:
        raise HTTPException(status_code=502, detail={"webhook_status": response.status_code, "result": result})
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail={"webhook_status": response.status_code, "result": result})

    _last_demo_delivery = {
        "body": body,
        "delivery_id": delivery_id,
        "signature": signature,
        "headers": headers,
        "action": action,
        "commit_sha": commit_sha,
    }
    return {
        "event": "pull_request",
        "action": action,
        "delivery_id": delivery_id,
        "commit_sha": commit_sha,
        "webhook_http_status": response.status_code,
        "result": result,
    }


@app.post("/demo-api/replay-last")
async def replay_last_demo_delivery():
    """Redeliver identical bytes and delivery ID to show idempotent handling."""
    _require_development_demo()
    if _last_demo_delivery is None:
        raise HTTPException(status_code=409, detail="Send a sample PR event before replaying it")
    try:
        response = await _http_client.post(
            f"{WEBHOOK_SERVICE_URL}/webhook/github",
            content=_last_demo_delivery["body"],
            headers=_last_demo_delivery["headers"],
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Webhook service unavailable: {exc}") from exc
    try:
        result = response.json()
    except ValueError:
        result = {"detail": response.text[:300]}
    return {
        "event": "pull_request replay",
        "action": _last_demo_delivery["action"],
        "delivery_id": _last_demo_delivery["delivery_id"],
        "commit_sha": _last_demo_delivery["commit_sha"],
        "webhook_http_status": response.status_code,
        "result": result,
    }


def _require_development_demo() -> None:
    if ENVIRONMENT in {"prod", "production"}:
        raise HTTPException(status_code=404, detail="Demo endpoints are disabled outside development")


@app.api_route("/api/learner/{path:path}", methods=["GET", "POST"])
async def route_to_learner(path: str, request: Request, _claims: dict = Depends(require_auth)):
    """Authenticated gateway to the learner; inject the internal service token."""
    return await _proxy(
        request,
        f"{LEARNER_SERVICE_URL}/{path}",
        extra_headers={"X-Worker-Token": WORKER_STATUS_TOKEN},
    )


@app.api_route("/webhook/{path:path}", methods=["POST", "GET"])
async def route_to_webhook_service(path: str, request: Request):
    """Unauthenticated passthrough; GitHub HMAC is verified downstream."""
    return await _proxy(request, f"{WEBHOOK_SERVICE_URL}/webhook/{path}")


@app.api_route(
    "/api/orchestrator/{path:path}", methods=["GET", "POST", "PUT", "DELETE"]
)
async def route_to_orchestrator(
    path: str, request: Request, _claims: dict = Depends(require_auth)  # noqa: B008
):
    """Authenticated passthrough to the Orchestrator service."""
    return await _proxy(request, f"{ORCHESTRATOR_SERVICE_URL}/{path}")


async def _proxy(request: Request, target_url: str, extra_headers: dict[str, str] | None = None) -> Response:
    body = await request.body()
    try:
        upstream_response = await _http_client.request(
            method=request.method,
            url=target_url,
            headers={
                **{k: v for k, v in request.headers.items() if k.lower() not in {"host", "content-length"}},
                **(extra_headers or {}),
            },
            content=body,
            params=request.query_params,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Upstream service unavailable: {exc}") from exc
    return Response(
        content=upstream_response.content,
        status_code=upstream_response.status_code,
        headers={
            k: v
            for k, v in upstream_response.headers.items()
            if k.lower() not in ("content-length", "transfer-encoding", "connection")
        },
    )
