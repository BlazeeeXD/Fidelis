"""Small HTTP integrations used by the review pipeline.

The reviewer speaks to any OpenAI-compatible chat-completions endpoint (OpenAI,
Ollama, LM Studio, llama.cpp server, vLLM, etc.) and GitHub's REST API. Keeping
these transports behind tiny clients makes local inference and test doubles easy.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import re
from typing import Any
from urllib.parse import quote

import httpx

from app.schemas import ErrorCode, ReviewerError

logger = logging.getLogger("reviewer.integrations")


class GitHubClient:
    def __init__(self, token: str | None, *, api_url: str = "https://api.github.com", timeout: float = 30.0):
        self.token = token or ""
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout

    def _headers(self, accept: str = "application/vnd.github+json") -> dict[str, str]:
        headers = {
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "Fidelis-PR-Reviewer",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def fetch_diff(self, repository: str, pr_number: int) -> str:
        if not self.token:
            raise ReviewerError(
                ErrorCode.UPSTREAM_ERROR,
                "GITHUB_TOKEN is required to fetch a PR diff. Supply `diff` for offline/manual reviews.",
                retryable=False,
            )
        url = f"{self.api_url}/repos/{quote(repository, safe='/')}/pulls/{pr_number}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(url, headers=self._headers("application/vnd.github.v3.diff"))
        except httpx.TimeoutException as exc:
            raise ReviewerError(ErrorCode.REVIEW_TIMEOUT, "Timed out while fetching the GitHub PR diff.") from exc
        except httpx.HTTPError as exc:
            raise ReviewerError(ErrorCode.UPSTREAM_ERROR, f"GitHub diff request failed: {type(exc).__name__}") from exc
        if response.status_code in {401, 403}:
            raise ReviewerError(ErrorCode.UPSTREAM_ERROR, "GitHub rejected the configured token or its permissions.", retryable=False)
        if response.status_code == 404:
            raise ReviewerError(ErrorCode.INVALID_REQUEST, "GitHub pull request was not found or is not accessible.")
        if response.status_code == 429 or response.status_code >= 500:
            raise ReviewerError(ErrorCode.UPSTREAM_ERROR, f"GitHub diff request returned HTTP {response.status_code}.")
        if not response.is_success:
            raise ReviewerError(ErrorCode.INVALID_REQUEST, f"GitHub diff request returned HTTP {response.status_code}.")
        diff = response.text
        if not diff.strip():
            return ""
        if len(diff) > 2_000_000:
            raise ReviewerError(ErrorCode.INVALID_REQUEST, "GitHub diff exceeds the 2,000,000 character review limit.")
        return diff

    async def fetch_merged_pull_requests(self, repository: str, *, per_page: int = 30) -> list[dict[str, Any]]:
        if not self.token:
            raise ReviewerError(ErrorCode.UPSTREAM_ERROR, "GITHUB_TOKEN is required for repository learning.", retryable=False)
        url = f"{self.api_url}/repos/{quote(repository, safe='/')}/pulls"
        params = {"state": "closed", "sort": "updated", "direction": "desc", "per_page": min(max(per_page, 1), 100)}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(url, headers=self._headers(), params=params)
        except httpx.HTTPError as exc:
            raise ReviewerError(ErrorCode.UPSTREAM_ERROR, f"GitHub PR listing failed: {type(exc).__name__}") from exc
        if response.status_code in {401, 403}:
            raise ReviewerError(ErrorCode.UPSTREAM_ERROR, "GitHub rejected the configured token or its permissions.", retryable=False)
        if response.status_code == 429 or response.status_code >= 500:
            raise ReviewerError(ErrorCode.UPSTREAM_ERROR, f"GitHub PR listing returned HTTP {response.status_code}.")
        if not response.is_success:
            raise ReviewerError(ErrorCode.INVALID_REQUEST, f"GitHub PR listing returned HTTP {response.status_code}.")
        return [item for item in response.json() if item.get("merged_at")]

    async def fetch_pull_diff(self, repository: str, pr_number: int) -> str:
        return await self.fetch_diff(repository, pr_number)

    async def fetch_file(self, repository: str, path: str, ref: str) -> str | None:
        """Fetch a text file as it exists at an exact commit SHA.

        The caller treats this as optional context: missing files, binary files,
        oversized files and permission limitations should not make a valid PR
        diff unreviewable. Authentication is still required for private repos.
        """
        if not self.token:
            return None
        url = f"{self.api_url}/repos/{quote(repository, safe='/')}/contents/{quote(path, safe='/')}"
        try:
            async with httpx.AsyncClient(timeout=min(self.timeout, 12.0)) as client:
                response = await client.get(
                    url,
                    headers=self._headers("application/vnd.github.raw+json"),
                    params={"ref": ref},
                )
        except httpx.HTTPError as exc:
            raise ReviewerError(
                ErrorCode.UPSTREAM_ERROR,
                f"GitHub source-context request failed: {type(exc).__name__}",
            ) from exc
        if response.status_code == 404:
            return None
        if response.status_code in {401, 403}:
            raise ReviewerError(
                ErrorCode.UPSTREAM_ERROR,
                "GitHub does not permit reading source files with the configured token.",
                retryable=False,
            )
        if response.status_code == 429 or response.status_code >= 500:
            raise ReviewerError(
                ErrorCode.UPSTREAM_ERROR,
                f"GitHub source-context request returned HTTP {response.status_code}.",
            )
        if not response.is_success:
            return None

        content_type = response.headers.get("content-type", "").lower()
        if "application/vnd.github.raw" not in content_type and "json" in content_type:
            try:
                payload = response.json()
                encoded = payload.get("content") if isinstance(payload, dict) else None
                if not isinstance(encoded, str) or payload.get("encoding") != "base64":
                    return None
                raw = base64.b64decode(encoded, validate=False)
                text = raw.decode("utf-8")
            except (ValueError, TypeError, UnicodeDecodeError, binascii.Error):
                return None
        else:
            try:
                text = response.content.decode("utf-8")
            except UnicodeDecodeError:
                return None
        if "\x00" in text:
            return None
        if len(text) > 2_000_000:
            return None
        return text


class OpenAICompatibleClient:
    """OpenAI-compatible Chat Completions client with bounded error handling."""

    def __init__(self, *, base_url: str, api_key: str, model: str, timeout: float = 180.0, temperature: float = 0.1, reasoning_effort: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.temperature = temperature
        self.reasoning_effort = reasoning_effort

    async def json_completion(self, *, system: str, user: str, max_tokens: int = 2500) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        # Google maps OpenAI's reasoning_effort to Gemini thinking_level. Do not
        # send this provider-specific parameter to local llama.cpp/OpenAI APIs.
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(f"{self.base_url}/chat/completions", headers=headers, json=body)
        except httpx.TimeoutException as exc:
            # Do not let the job worker blindly replay paid/free-tier inference
            # requests after a client timeout. A timeout may occur after the
            # provider has already processed the generation.
            raise ReviewerError(
                ErrorCode.REVIEW_TIMEOUT,
                f"LLM request timed out ({type(exc).__name__}) after {self.timeout:g}s; no automatic replay was attempted.",
                retryable=False,
                details={"timeout_seconds": self.timeout, "timeout_type": type(exc).__name__},
            ) from exc
        except httpx.HTTPError as exc:
            raise ReviewerError(
                ErrorCode.REVIEWER_UNAVAILABLE,
                f"LLM endpoint unavailable: {type(exc).__name__}; no automatic replay was attempted.",
                retryable=False,
                details={"transport_error": type(exc).__name__},
            ) from exc

        if not response.is_success:
            provider_body = _safe_provider_error_body(response.text, self.api_key)
            request_id = _provider_request_id(response.headers)
            details = {"provider_status": response.status_code}
            if request_id:
                details["provider_request_id"] = request_id
            if provider_body:
                details["provider_error"] = provider_body

            if response.status_code in {401, 403}:
                raise ReviewerError(
                    ErrorCode.UPSTREAM_ERROR,
                    f"LLM provider rejected the API key or permissions (HTTP {response.status_code}). {provider_body}".strip(),
                    retryable=False, details=details,
                )
            if response.status_code == 429:
                raise ReviewerError(
                    ErrorCode.UPSTREAM_ERROR,
                    f"LLM provider rate/quota limit (HTTP 429). No automatic retry was attempted to protect usage. {provider_body}".strip(),
                    retryable=False, details=details,
                )
            if response.status_code == 503:
                raise ReviewerError(
                    ErrorCode.REVIEWER_UNAVAILABLE,
                    f"LLM provider is temporarily unavailable/overloaded (HTTP 503). No automatic retry was attempted to protect usage. {provider_body}".strip(),
                    retryable=False, details=details,
                )
            if response.status_code == 504:
                raise ReviewerError(
                    ErrorCode.REVIEW_TIMEOUT,
                    f"LLM provider deadline exceeded (HTTP 504). No automatic retry was attempted to protect usage. {provider_body}".strip(),
                    retryable=False, details=details,
                )
            if response.status_code >= 500:
                raise ReviewerError(
                    ErrorCode.UPSTREAM_ERROR,
                    f"LLM provider returned HTTP {response.status_code}. No automatic retry was attempted. {provider_body}".strip(),
                    retryable=False, details=details,
                )
            raise ReviewerError(
                ErrorCode.INVALID_REQUEST,
                f"LLM endpoint returned HTTP {response.status_code}. {provider_body}".strip(),
                retryable=False, details=details,
            )
        try:
            envelope = response.json()
            choice = envelope["choices"][0]
            finish_reason = choice.get("finish_reason")
            if finish_reason == "length":
                raise ReviewerError(
                    ErrorCode.INVALID_RESPONSE,
                    "LLM output was truncated at the token limit. Increase LLM_MAX_TOKENS or reduce the review input.",
                    retryable=False,
                )
            content = choice["message"]["content"]
            if isinstance(content, list):
                content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
            if isinstance(content, dict):
                content = dict(content)
                content.pop("_usage", None)
                usage = envelope.get("usage") if isinstance(envelope, dict) else None
                if isinstance(usage, dict):
                    content["_usage"] = {
                        "input_tokens": usage.get("prompt_tokens", usage.get("input_tokens")),
                        "output_tokens": usage.get("completion_tokens", usage.get("output_tokens")),
                    }
                return content
            parsed = _extract_json_object(str(content))
            parsed.pop("_usage", None)
            # Usage is retained as private transport metadata, never trusted as
            # model-authored findings and stripped before contract validation.
            usage = envelope.get("usage") if isinstance(envelope, dict) else None
            if isinstance(usage, dict):
                parsed["_usage"] = {
                    "input_tokens": usage.get("prompt_tokens", usage.get("input_tokens")),
                    "output_tokens": usage.get("completion_tokens", usage.get("output_tokens")),
                }
            return parsed
        except ReviewerError:
            raise
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            logger.warning("LLM returned a non-contract JSON response: %s", type(exc).__name__)
            raise ReviewerError(ErrorCode.INVALID_RESPONSE, "LLM response was not valid JSON with a findings array.", retryable=False) from exc


def _provider_request_id(headers: Any) -> str | None:
    """Return a non-secret upstream correlation ID when the provider supplies one."""
    for name in ("x-goog-request-id", "x-guploader-uploadid", "request-id", "x-request-id"):
        value = headers.get(name) if hasattr(headers, "get") else None
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
    return None


def _safe_provider_error_body(body: str, api_key: str) -> str:
    """Keep useful provider diagnostics while ensuring credentials are never echoed."""
    text = (body or "").strip()
    if api_key:
        text = text.replace(api_key, "[REDACTED_API_KEY]")
    text = re.sub(r"AIza[0-9A-Za-z_\-]{20,}", "[REDACTED_API_KEY]", text)
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+", r"\1[REDACTED]", text)
    return text[:1000]


def _extract_json_object(content: str) -> dict[str, Any]:
    """Extract one JSON object from common OpenAI/local-model wrappers.

    Some local reasoning models prepend a ``<think>`` block or wrap the JSON
    in Markdown fences even when JSON mode is requested. Ignore those wrappers
    without relaxing the requirement that the payload itself be a JSON object.
    """
    text = content.strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.IGNORECASE | re.DOTALL).strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```\s*$", "", text)
    if text.lower().startswith("json"):
        text = text[4:].lstrip("\r\n :")
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            parsed, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("no JSON object found in model content")
