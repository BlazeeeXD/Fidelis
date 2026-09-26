from fastapi import FastAPI

from app.api import jobs

app = FastAPI(title="Orchestrator Service", version="0.1.0")

app.include_router(jobs.router)


@app.get("/health")
async def health():
    return {"status": "healthy"}