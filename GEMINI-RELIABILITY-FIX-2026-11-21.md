# Gemini provider reliability update — 2026-11-21

## Diagnosis

The user's Gemini 3.8 Flash API calls reached Google's Gemini Developer API but failed with HTTP 503 Service Unavailable and HTTP 504 Gateway Timeout. The same 503/504 symptoms were being reported by other developers around 17–20 November 2026, including minimal prompts and direct REST calls. Google Developer Forum support responses attributed 503 to temporary serving overload. This is an upstream availability problem, not a local Docker, network, API-key, or Fidelis prompt defect.

References:
- Google Gemini API errors: https://ai.google.dev/gemini-api/docs/api-errors
- Google OpenAI compatibility: https://ai.google.dev/gemini-api/docs/openai
- Gemini Developer API pricing/model tiers: https://ai.google.dev/gemini-api/docs/pricing
- Recent 3.8 Flash API overload report: https://discuss.ai.google.dev/t/gemini-3-8-flash-api-returns-503-but-ai-studio-works/187076
- Recent report of 3.8 Flash failing while 3.5 Flash-Lite succeeded: https://discuss.ai.google.dev/t/gemini-3-8-flash-api-returns-503-but-ai-studio-works/187076

## Changes

- Switch `.env.gemini.example` to `gemini-3.5-flash-lite`, a lower-cost model with a free API tier listing and a minimal default reasoning mode. This is a fallback experiment, not a guarantee that provider capacity will be available.
- Permit `LLM_REASONING_EFFORT=minimal` in reviewer configuration.
- Reduce Gemini smoke test to one 512-token generation request with a 45-second bound. No retry is performed.
- Provider 429/503/504 and client timeouts are marked non-retryable by default so Celery does not automatically repeat generation requests and waste usage.
- Preserve sanitized provider error body and request correlation ID, when supplied, in the error detail. API keys are redacted.
- Add regression coverage for minimal reasoning, sanitized provider errors, and no-retry behavior.

## Deployment

Copy `.env.gemini.example` values into the existing root `.env`, preserving all other credentials. Run:

```cmd
docker compose up -d --build --force-recreate reviewer
```

Then run exactly one smoke test:

```cmd
docker compose exec reviewer python -m app.gemini_smoke_test
```

Only if it returns PASS should a real PR review be started. If the provider returns 503/504 again, stop: do not repeatedly rerun the test. The error now includes the sanitized upstream response for diagnosis.

To switch back to Gemini 3.8 Flash later, change `LLM_MODEL=gemini-3.8-flash` and `LLM_REASONING_EFFORT=low` after the API endpoint is responding reliably.
