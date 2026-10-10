"""Main entry point for the Orchestrator service."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import jobs
from app.db import models  # Register ORM models with SQLAlchemy metadata
from app.db.database import Base, engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create database tables when the service starts."""
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(
    title="Orchestrator Service",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(jobs.router)


@app.get("/health")
async def health():
    return {"status": "healthy"}
