import asyncio
import json

import httpx
import pytest

from app.orchestrator_client import (
    OrchestratorClient,
    OrchestratorError,
    OrchestratorRejectedError,
)


def make_client(handler):
    return OrchestratorClient(
        base_url="http://orch", backoff=0, transport=httpx.MockTransport(handler)
    )


def test_success_sends_contract_payload():
    seen = {}

    def handler(request: httpx.Request):
        seen["json"] = json.loads(request.content)
        seen["path"] = request.url.path
        return httpx.Response(201, json={"job_id": "j1", "status": "QUEUED"})

    result = asyncio.run(make_client(handler).create_job("a/b", 7, "sha1"))

    assert result["job_id"] == "j1"
    assert seen["path"] == "/jobs"
    assert seen["json"] == {"repository": "a/b", "pr_number": 7, "commit_sha": "sha1"}


def test_duplicate_200_is_a_success():
    def handler(request):
        return httpx.Response(
            200, json={"job_id": "j1", "status": "QUEUED", "duplicate": True}
        )

    result = asyncio.run(make_client(handler).create_job("a/b", 1, "s"))
    assert result["duplicate"] is True


def test_retries_on_5xx_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(201, json={"job_id": "j1", "status": "QUEUED"})

    result = asyncio.run(make_client(handler).create_job("a/b", 1, "s"))
    assert result["job_id"] == "j1"
    assert calls["n"] == 3


def test_retries_on_connection_error():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(201, json={"job_id": "j1", "status": "QUEUED"})

    result = asyncio.run(make_client(handler).create_job("a/b", 1, "s"))
    assert result["job_id"] == "j1"
    assert calls["n"] == 2


def test_gives_up_after_retries():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(500)

    with pytest.raises(OrchestratorError):
        asyncio.run(make_client(handler).create_job("a/b", 1, "s"))
    assert calls["n"] == 3  # 1 try + 2 retries


def test_no_retry_on_4xx():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(422, json={"detail": "bad"})

    with pytest.raises(OrchestratorRejectedError):
        asyncio.run(make_client(handler).create_job("a/b", 1, "s"))
    assert calls["n"] == 1