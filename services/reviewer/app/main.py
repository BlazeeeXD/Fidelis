# PLACEHOLDER by P5 so compose can start. P1 replaces this with the real reviewer service.
from fastapi import FastAPI

app = FastAPI(title="PR Reviewer - Reviewer Service")


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
