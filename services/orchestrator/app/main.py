"""Main entry point for the Orchestrator service."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import inspect

from app.api import jobs, repositories
from app.db import models  # noqa: F401
from app.db.database import Base, engine
from app.db.models import ReviewJobDB
from app.services.queue_dispatch import dispatch_pending_jobs

logger = logging.getLogger("orchestrator.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create database tables when the service starts."""
    Base.metadata.create_all(bind=engine)
    # create_all() does not add indexes to an already existing table. Inspect
    # the reflected predicate and only repair a stale/missing active-job index;
    # dropping/recreating it on every restart needlessly locks a busy table.
    reflected = inspect(engine).get_indexes("review_jobs")
    for index in ReviewJobDB.__table__.indexes:
        existing = next((item for item in reflected if item["name"] == index.name), None)
        where_key = f"{engine.dialect.name}_where"
        predicate = str((existing or {}).get("dialect_options", {}).get(where_key, "")).upper()
        is_current = all(token in predicate for token in ("STATUS", "QUEUED", "RUNNING"))
        if existing is not None and is_current:
            continue
        if existing is not None:
            logger.warning("repairing stale database index %s", index.name)
            index.drop(bind=engine, checkfirst=True)
        index.create(bind=engine, checkfirst=True)
    sweep_enabled = os.getenv("ASYNC_REVIEW_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
    stop_event = asyncio.Event()
    sweep_task = None

    async def outbox_loop():
        while not stop_event.is_set():
            try:
                await asyncio.to_thread(dispatch_pending_jobs, limit=50)
            except Exception:
                logger.exception("outbox recovery sweep failed")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=5.0)
            except TimeoutError:
                pass

    if sweep_enabled:
        sweep_task = asyncio.create_task(outbox_loop(), name="fidelis-outbox-sweeper")
    try:
        yield
    finally:
        if sweep_task is not None:
            stop_event.set()
            sweep_task.cancel()
            try:
                await sweep_task
            except asyncio.CancelledError:
                pass


app = FastAPI(
    title="Orchestrator Service",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(jobs.router)
app.include_router(repositories.router)


@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.get("/metrics/summary")
def metrics_summary(repository: str | None = None):
    return jobs.orchestrator_service.summary_metrics(repository)


@app.get("/metrics", include_in_schema=False)
def prometheus_metrics():
    """Expose standard Prometheus text format without a push side channel."""
    from fastapi.responses import Response
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        CollectorRegistry,
        Gauge,
        generate_latest,
    )

    registry = CollectorRegistry()
    data = jobs.orchestrator_service.summary_metrics()
    for name, value in (
        ("fidelis_review_jobs_total", data["total_jobs"]),
        ("fidelis_review_jobs_queued", data["queued_jobs"]),
        ("fidelis_review_jobs_running", data["running_jobs"]),
        ("fidelis_review_jobs_completed", data["completed_jobs"]),
        ("fidelis_review_jobs_failed", data["failed_jobs"]),
        ("fidelis_review_findings_total", data["total_findings"]),
        ("fidelis_llm_input_tokens_total", data["total_input_tokens"]),
        ("fidelis_llm_output_tokens_total", data["total_output_tokens"]),
    ):
        gauge = Gauge(name, name.replace("_", " "), registry=registry)
        gauge.set(value)
    agent_gauge = Gauge(
        "fidelis_agent_average_duration_ms",
        "Average runtime by review agent in milliseconds",
        labelnames=["agent"], registry=registry,
    )
    for agent, duration in data["average_agent_duration_ms"].items():
        agent_gauge.labels(agent=agent).set(duration)
    return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)
