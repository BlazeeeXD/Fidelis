"""Verify Gemini API connectivity using Fidelis' production LLM HTTP client.

Run inside the reviewer container:
    python -m app.gemini_smoke_test

The script never prints the API key. It checks HTTP connectivity, model access,
JSON mode, and the top-level findings[] contract before any GitHub review runs.
"""
from __future__ import annotations

import asyncio
import sys
import time
from urllib.parse import urlparse

from app.config import Settings
from app.integrations import OpenAICompatibleClient


async def _run() -> int:
    settings = Settings.from_env()
    parsed = urlparse(settings.llm_base_url)

    if not settings.llm_api_key:
        print("FAIL: LLM_API_KEY is empty. Add your Gemini API key to the root .env.")
        return 2
    if parsed.hostname != "generativelanguage.googleapis.com":
        print(
            "FAIL: LLM_BASE_URL is not the Gemini API endpoint. "
            "Expected https://generativelanguage.googleapis.com/v1beta/openai"
        )
        print(f"Current host: {parsed.hostname or '(missing)'}")
        return 2
    if not settings.llm_model:
        print("FAIL: LLM_MODEL is empty.")
        return 2

    client = OpenAICompatibleClient(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        # One generation attempt only. This is deliberately short so a model
        # availability issue does not burn time or fan out into four agent calls.
        timeout=min(settings.llm_timeout_seconds, 45.0),
        temperature=0.0,
        reasoning_effort=settings.llm_reasoning_effort,
    )

    print("Fidelis Gemini API smoke test")
    print(f"Endpoint host: {parsed.hostname}")
    print(f"Model: {settings.llm_model}")
    print("API key: configured (value hidden)")
    print(f"Reasoning effort: {settings.llm_reasoning_effort}")
    print("Sending ONE small JSON-mode request (45-second maximum; no retries)...", flush=True)
    started = time.perf_counter()

    try:
        response = await client.json_completion(
            system=(
                "You are testing a JSON response contract. Do not analyze code. "
                'Return exactly one JSON object: {"findings": []}. '
                "Do not include markdown or any other text."
            ),
            user='Return exactly this JSON object and nothing else: {"findings": []}',
            max_tokens=512,
        )
    except Exception as exc:  # noqa: BLE001 - print safe diagnostic, never secrets
        elapsed = time.perf_counter() - started
        print(f"FAIL after {elapsed:.1f}s: {type(exc).__name__}: {exc}")
        return 1

    elapsed = time.perf_counter() - started
    print(f"Response received in {elapsed:.1f}s", flush=True)
    findings = response.get("findings") if isinstance(response, dict) else None
    usage = response.get("_usage", {}) if isinstance(response, dict) else {}
    if not isinstance(findings, list):
        keys = sorted(response.keys()) if isinstance(response, dict) else []
        print(f"FAIL: Gemini returned JSON, but not Fidelis' findings[] shape. Keys: {keys}")
        print(f"Response (truncated): {str(response)[:600]}")
        return 1

    print("PASS: Gemini authenticated and generated a response.")
    print("PASS: Fidelis parsed JSON successfully.")
    print(f"PASS: Required findings[] array present ({len(findings)} test findings).")
    if isinstance(usage, dict):
        print(f"Input tokens: {usage.get('input_tokens', 'not reported')}")
        print(f"Output tokens: {usage.get('output_tokens', 'not reported')}")
    print("No GitHub PR was created or modified by this test.")
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
