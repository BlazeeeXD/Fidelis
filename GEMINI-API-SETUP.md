# Fidelis + Google Gemini API

Fidelis already uses an OpenAI-compatible Chat Completions transport. Gemini's
OpenAI-compatibility endpoint can therefore be used without changing the review
pipeline or adding another SDK. The profile currently starts with `gemini-3.5-flash-lite`
to avoid the reported 3.8 Flash Developer API serving overload. Switch to
`gemini-3.8-flash` when that endpoint is responding reliably.

## 1. Get an API key

Create a Gemini API key in Google AI Studio:

https://aistudio.google.com/apikey

Do not paste the key into chat, GitHub, screenshots, or source control.

## 2. Change only the model-provider values

Open the root `.env` beside `docker-compose.yml`. Preserve all existing GitHub,
webhook, database, JWT, Redis and worker secrets. Change only these values:

```dotenv
REVIEWER_MODE=llm
REVIEW_AGENTS=STATIC_ANALYSIS,SECURITY,ARCHITECTURE,CODE_STYLE
REVIEW_EXECUTION_STRATEGY=sequential
REVIEW_CHECKPOINTS_ENABLED=true
LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
LLM_API_KEY=YOUR_GEMINI_API_KEY
LLM_MODEL=gemini-3.5-flash-lite
LLM_TIMEOUT_SECONDS=120
LLM_REASONING_EFFORT=minimal
LLM_TEMPERATURE=0.1
LLM_MAX_TOKENS=4096
```

The endpoint intentionally has no trailing `/chat/completions`; Fidelis appends
that path itself. The base URL and Bearer authentication match Google's official
OpenAI-compatible Chat Completions documentation.

## 3. Rebuild/recreate only the reviewer

From the Fidelis project directory in CMD:

```cmd
docker compose up -d --build --force-recreate reviewer
```

The `review-worker` calls the Reviewer HTTP service; it does not make model API
requests itself. It does not need the Gemini API key.

## 4. Test Gemini before opening/reopening a PR

```cmd
docker compose exec reviewer python -m app.gemini_smoke_test
```

The smoke test performs exactly one generation request, uses a 512-token cap,
`reasoning_effort=minimal`, and a 45-second timeout. It does not automatically
retry on 429/503/504 or transport timeout, to avoid duplicate inference usage.
The production client also surfaces a sanitized provider error body and request
correlation ID when available, and marks provider availability failures as
non-retryable by default.

Why Flash-Lite: Gemini 3.8 Flash Developer API requests have recently been
reported to return 503 overload and 504 deadline errors even for minimal
requests. Flash-Lite is a lower-cost free-tier-listed fallback model; it is a
connectivity/control experiment, not a claim that its review quality matches
3.8 Flash. If it works, later run the same fixture once to compare quality.

Expected output includes:

```text
PASS: Gemini authenticated and generated a response.
PASS: Fidelis parsed JSON successfully.
PASS: Required findings[] array present (0 test findings).
```

This smoke test does not call GitHub and does not create a review job. It uses
the same `OpenAICompatibleClient` and JSON-mode request path used by the real
reviewer.

## 5. Run the existing vulnerable PR

Keep Docker and ngrok running. The local llama.cpp server is no longer needed
for this Gemini test and can be stopped to free VRAM.

Push a new empty commit on the existing vulnerable test branch to produce a
new head SHA and a fresh `synchronize` event:

```cmd
git commit --allow-empty -m "Run Fidelis using Gemini API"
git push
```

Watch the pipeline:

```cmd
docker compose logs -f --since=2m webhook orchestrator review-worker reviewer
```

Expected: four sequential specialist passes, one completed review, then one
GitHub PR comment. Compare detection against the same planted test cases used
for the DeepSeek baseline.

## Provider switching

To return to the local DeepSeek server, restore the previous values:

```dotenv
LLM_BASE_URL=http://host.docker.internal:8080/v1
LLM_API_KEY=YOUR_LOCAL_LLAMA_CPP_KEY
LLM_MODEL=fidelis-deepseek14b
```

Then recreate the reviewer again.

## Important plan and data notes

A Google AI Pro subscription is not itself a blanket pool of Gemini Developer
API tokens. Gemini API usage is governed by the API project's tier, model
availability and rate limits. Google currently lists Gemini 3.8 Flash on the
Gemini API free tier; quotas are still enforced and can change. Check the live
pricing/rate-limit page for the API project before heavy use:

https://ai.google.dev/gemini-api/docs/pricing

Google's free API tier terms state that submitted content may be used to improve
Google products. Fidelis sends PR diffs and selected source excerpts to the
provider. Use only repositories/source code you are permitted to send to that
service. Paid-tier API data handling differs; review Google's current terms.

Official OpenAI compatibility documentation:
https://ai.google.dev/gemini-api/docs/openai
