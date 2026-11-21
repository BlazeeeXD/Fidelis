import asyncio

import pytest
from app.integrations import OpenAICompatibleClient
from app.schemas import ErrorCode, ReviewerError


class _Response:
    status_code = 200
    is_success = True

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _AsyncClient:
    payload = None
    last_request = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, *args, **kwargs):
        type(self).last_request = (args, kwargs)
        return _Response(self.payload)


def _client():
    return OpenAICompatibleClient(
        base_url="http://localhost:8080/v1",
        api_key="test",
        model="test-model",
    )


def test_gemini_reasoning_effort_is_sent_when_configured(monkeypatch):
    _AsyncClient.payload = {
        "choices": [{"finish_reason": "stop", "message": {"content": '{"findings": []}'}}]
    }
    monkeypatch.setattr("app.integrations.httpx.AsyncClient", _AsyncClient)
    client = OpenAICompatibleClient(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        api_key="test",
        model="gemini-3.8-flash",
        reasoning_effort="low",
    )

    result = asyncio.run(client.json_completion(system="test", user="return findings", max_tokens=1024))

    assert result["findings"] == []
    request_body = _AsyncClient.last_request[1]["json"]
    assert request_body["reasoning_effort"] == "low"
    assert request_body["max_tokens"] == 1024


def test_local_provider_does_not_receive_reasoning_effort_by_default(monkeypatch):
    _AsyncClient.payload = {
        "choices": [{"finish_reason": "stop", "message": {"content": '{"findings": []}'}}]
    }
    monkeypatch.setattr("app.integrations.httpx.AsyncClient", _AsyncClient)

    asyncio.run(_client().json_completion(system="test", user="return findings"))

    request_body = _AsyncClient.last_request[1]["json"]
    assert "reasoning_effort" not in request_body


def test_json_completion_extracts_object_after_deepseek_think_block_and_fence(monkeypatch):
    _AsyncClient.payload = {
        "choices": [{
            "finish_reason": "stop",
            "message": {"content": '<think>Consider the patch carefully.</think>\n```json\n{"findings": []}\n```'},
        }],
        "usage": {"prompt_tokens": 12, "completion_tokens": 4},
    }
    monkeypatch.setattr("app.integrations.httpx.AsyncClient", _AsyncClient)

    result = asyncio.run(_client().json_completion(system="review", user="diff"))

    assert result["findings"] == []
    assert result["_usage"] == {"input_tokens": 12, "output_tokens": 4}


def test_model_cannot_forge_usage_metadata(monkeypatch):
    _AsyncClient.payload = {
        "choices": [{
            "finish_reason": "stop",
            "message": {"content": '{"findings": [], "_usage": {"input_tokens": 999999}}'},
        }]
    }
    monkeypatch.setattr("app.integrations.httpx.AsyncClient", _AsyncClient)

    result = asyncio.run(_client().json_completion(system="review", user="diff"))

    assert result == {"findings": []}


def test_json_completion_fails_explicitly_when_provider_truncates_output(monkeypatch):
    _AsyncClient.payload = {
        "choices": [{"finish_reason": "length", "message": {"content": '{"findings": ['}}]
    }
    monkeypatch.setattr("app.integrations.httpx.AsyncClient", _AsyncClient)

    with pytest.raises(ReviewerError) as error:
        asyncio.run(_client().json_completion(system="review", user="diff"))

    assert error.value.code is ErrorCode.INVALID_RESPONSE
    assert "token limit" in error.value.message


class _GitHubResponse:
    def __init__(self, status_code=200, content=b"def reviewed():\n    return True\n", content_type="application/vnd.github.raw"):
        self.status_code = status_code
        self.is_success = 200 <= status_code < 300
        self.content = content
        self.headers = {"content-type": content_type}


class _GitHubAsyncClient:
    response = _GitHubResponse()
    request = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, **kwargs):
        type(self).request = (url, kwargs)
        return type(self).response


def test_github_fetch_file_uses_exact_commit_ref_and_returns_raw_text(monkeypatch):
    from app.integrations import GitHubClient

    _GitHubAsyncClient.response = _GitHubResponse()
    monkeypatch.setattr("app.integrations.httpx.AsyncClient", _GitHubAsyncClient)

    content = asyncio.run(GitHubClient("test-token").fetch_file("owner/repo", "src/api.py", "head-sha"))

    assert content == "def reviewed():\n    return True\n"
    url, request = _GitHubAsyncClient.request
    assert url.endswith("/repos/owner/repo/contents/src/api.py")
    assert request["params"] == {"ref": "head-sha"}
    assert "Authorization" in request["headers"]


def test_github_fetch_file_skips_deleted_or_missing_content(monkeypatch):
    from app.integrations import GitHubClient

    _GitHubAsyncClient.response = _GitHubResponse(status_code=404)
    monkeypatch.setattr("app.integrations.httpx.AsyncClient", _GitHubAsyncClient)

    content = asyncio.run(GitHubClient("test-token").fetch_file("owner/repo", "src/missing.py", "head-sha"))

    assert content is None


class _ErrorResponse:
    is_success = False

    def __init__(self, status_code, text, headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


def test_provider_503_exposes_sanitized_body_and_does_not_retry(monkeypatch):
    class ErrorClient(_AsyncClient):
        async def post(self, *args, **kwargs):
            return _ErrorResponse(
                503,
                '{"error":{"status":"UNAVAILABLE","message":"Model overloaded"}}',
                {"x-goog-request-id": "req-test-123"},
            )

    monkeypatch.setattr("app.integrations.httpx.AsyncClient", ErrorClient)
    client = OpenAICompatibleClient(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        api_key="secret-test-key",
        model="gemini-3.5-flash-lite",
    )

    with pytest.raises(ReviewerError) as error:
        asyncio.run(client.json_completion(system="test", user="test"))

    assert error.value.code is ErrorCode.REVIEWER_UNAVAILABLE
    assert error.value.retryable is False
    assert "Model overloaded" in error.value.message
    assert error.value.details["provider_request_id"] == "req-test-123"


def test_provider_error_redacts_api_key(monkeypatch):
    class ErrorClient(_AsyncClient):
        async def post(self, *args, **kwargs):
            return _ErrorResponse(403, '{"message":"bad secret-test-key"}')

    monkeypatch.setattr("app.integrations.httpx.AsyncClient", ErrorClient)
    client = OpenAICompatibleClient(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        api_key="secret-test-key",
        model="gemini-3.5-flash-lite",
    )

    with pytest.raises(ReviewerError) as error:
        asyncio.run(client.json_completion(system="test", user="test"))

    assert "secret-test-key" not in error.value.message
    assert "[REDACTED_API_KEY]" in error.value.message


def test_gemini_flash_lite_minimal_reasoning_is_sent_when_configured(monkeypatch):
    _AsyncClient.payload = {
        "choices": [{"finish_reason": "stop", "message": {"content": '{"findings": []}'}}]
    }
    monkeypatch.setattr("app.integrations.httpx.AsyncClient", _AsyncClient)
    client = OpenAICompatibleClient(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        api_key="test",
        model="gemini-3.5-flash-lite",
        reasoning_effort="minimal",
    )

    asyncio.run(client.json_completion(system="test", user="return findings", max_tokens=512))

    request_body = _AsyncClient.last_request[1]["json"]
    assert request_body["reasoning_effort"] == "minimal"
    assert request_body["max_tokens"] == 512
