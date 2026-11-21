# Fidelis — AI Pull Request Review System

Fidelis is an event-driven GitHub pull-request review system. It receives signed GitHub webhooks, creates durable review jobs, runs asynchronous analysis, stores structured findings and operational history, and can publish a review back to the pull request. It also mines merged pull requests for repository conventions and injects those conventions into future reviews.

This repository began as the Week 3 / V2 P3 delivery. It has been extended against the supplied 10-week roadmap through the V9 feature-freeze scope. The implementation is not a production-certified SaaS: external credentials, a working Docker host, and a live GitHub/LLM environment are required to validate the real deployment path. See [known limits](#known-limits) before presenting it as production-ready.

## What is implemented

| Roadmap milestone | Capability | Main implementation |
|---|---|---|
| V0–V1 | Service skeleton, GitHub webhook verification, PR event extraction, job creation and idempotency | `services/webhook`, `services/orchestrator`, `services/gateway` |
| V2 | Redis/Celery asynchronous review jobs, worker lifecycle, retries, durable transactional outbox and periodic outbox recovery | `services/reviewer/app/worker`, `services/orchestrator/app/services/queue_dispatch.py` |
| V3 | Single-pass LLM review using an OpenAI-compatible chat-completions endpoint | `services/reviewer/app/reviewers/llm.py` (`REVIEW_EXECUTION_STRATEGY=single_pass`) |
| V4 | Four independently configurable specialist reviewers with sequential, single-pass or parallel scheduling; deterministic aggregation and high-signal checks | `services/reviewer/app/reviewers/llm.py` |
| Review hardening | Security-focused specialist mode, source excerpts at the exact PR commit, changed-line validation and explicit malformed-output diagnostics | `services/reviewer/app/reviewers/llm.py` |
| V5 | One maintained, consolidated GitHub PR issue comment, with delivery outcomes recorded separately from analysis success | `services/reviewer/app/github_reviews.py` |
| V6 | Persisted result payloads, queryable findings, lifecycle/delivery audit events, replay endpoint, aggregate metrics and Control Room | Orchestrator DB/API and `services/gateway/app/static/index.html` |
| V7 | Background merged-PR miner that extracts explainable repository conventions and supplies them as reviewer context | `services/learner` |
| V8 | Prometheus metrics, provisioned Grafana dashboard, optional Langfuse v4 generation traces, retry and failure handling | `infra/monitoring`, `services/reviewer/app/observability.py` |
| V9 | Contract, service and integration tests; benchmark fixtures; pipeline evaluation and bounded load-test scripts | `services/*/tests`, `evaluation`, `scripts` |

The reviewer separates **which agents run** from **how the model is scheduled**. The default local profile runs four independent specialists sequentially through one already-loaded local model. Each completed specialist result is checkpointed in PostgreSQL so a retry can resume at the first unfinished pass when the exact review context is unchanged. `single_pass` asks one model generation to complete all selected perspectives and return a separate result for each; it is intended for cloud/API usage where one request reduces repeated prompt overhead. `parallel` remains available for inference servers with sufficient capacity. These strategies are explicit configuration choices, not different agent definitions.

## Architecture

```text
GitHub PR event
      │  HMAC SHA-256 verification
      ▼
Webhook service ── idempotency / event filtering
      │
      ▼
Orchestrator ── PostgreSQL job + outbox (single transaction)
      │
      ▼
Redis broker ── Celery review worker
      │
      ▼
Reviewer service
  ├─ fetch PR diff + repository rules
  ├─ build bounded context (diff + exact-commit source excerpts + repository rules)
  ├─ schedule selected specialists sequentially, in one shared call, or in parallel
  ├─ checkpoint completed specialist passes for retry/resume
  ├─ deterministic high-signal changed-line checks
  └─ validate, deduplicate, aggregate and report execution telemetry
      │
      ├── result + findings + agent telemetry + checkpoints ──► PostgreSQL
      └── one maintained PR report comment ───────────────────► GitHub API

Merged PRs ──► Learner worker ──► repository rules ──► future reviews

Orchestrator metrics ──► Prometheus ──► Grafana
LLM agent generations ──► optional Langfuse v4 (metadata only)
Gateway ──► browser Control Room + authenticated API proxy
```

### Services and host ports

| Container | Host port | Responsibility |
|---|---:|---|
| `gateway` | 8000 | Control Room, JWT auth, API proxy, development demo endpoints |
| `orchestrator` | 8001 | Job state machine, transactional outbox, results, findings, audit, repository rules, metrics |
| `webhook` | 8002 | GitHub signature verification, filtering, delivery dedupe and job handoff |
| `reviewer` | 8003 | Mock or LLM review API |
| `learner` | 8004 | Starts and reports repository-learning tasks |
| `postgres` | 5432 | Durable system of record |
| `redis` | internal | Celery broker and result backend |
| `review-worker` | internal | Runs queued PR reviews and reports status/result |
| `learning-worker` | internal | Fetches merged PRs and stores mined rules |
| `prometheus` | 9090 | Scrapes Orchestrator metrics |
| `grafana` | 3000 | Provisioned Fidelis overview dashboard |

## Quick start (local AI)

Prerequisites: Docker Desktop / Docker Engine with Compose v2, and Python 3.12+ for local scripts and tests.

```bash
cp .env.example .env
# Change every dev placeholder secret in .env before exposing any port.
docker compose up --build -d --wait
docker compose ps
```

On Windows PowerShell, the repository includes a startup wrapper that creates `.env` if needed, builds the compose stack, waits for healthchecks and runs the smoke test:

```powershell
.\scripts\dev-up.ps1
```

To stop without deleting data use `.\scripts\dev-down.ps1`; use `.\scripts\dev-down.ps1 -RemoveVolumes` only when you intend to delete all persisted database/cache/dashboard data.

Wait until Postgres, Redis, and the five HTTP services are healthy. The two Celery workers and Grafana/Prometheus have no application-level compose healthcheck. Open:

- Control Room: http://localhost:8000
- Grafana: http://localhost:3000 (default local dev login `admin` / `change-me`; change it)
- Prometheus: http://localhost:9090
- Reviewer API docs: http://localhost:8003/docs
- Orchestrator API docs: http://localhost:8001/docs

The sample `.env.example` is configured for a local OpenAI-compatible model: `REVIEWER_MODE=llm`, four specialists, sequential execution, and `http://host.docker.internal:8080/v1`. Start `llama-server` on the Windows host **before** sending a real review, listening on a host-reachable interface (for example `0.0.0.0:8080`) and exposing the OpenAI-compatible `/v1/chat/completions` endpoint. Set `LLM_MODEL` to the exact model alias returned by your server. The sample model name is `fidelis-deepseek14b`; change it if your server uses a different alias.

The smoke test is token-free: in LLM mode it checks service health and the active Reviewer mode but deliberately skips fake PR jobs, because those fixtures do not exist on GitHub. To exercise the entire synthetic webhook → queue → worker → result path, temporarily set `REVIEWER_MODE=mock`, restart the stack, and use the Control Room's **Send sample PR event** button. In normal LLM mode, send a real GitHub PR webhook for a repository the configured token can read; the sample button's synthetic repository is not a valid model-review target.

Run the standard smoke test:

```bash
python scripts/smoke_test.py
# Optional deeper PowerShell workflow checks on Windows (run with REVIEWER_MODE=mock)
powershell -ExecutionPolicy Bypass -File scripts/smoke_week2.ps1
powershell -ExecutionPolicy Bypass -File scripts/smoke_week3.ps1
```

## Enable real LLM review

The Reviewer accepts OpenAI-compatible `/chat/completions` endpoints. Configure `.env` and restart the stack:

```dotenv
REVIEWER_MODE=llm
REVIEW_AGENTS=STATIC_ANALYSIS,SECURITY,ARCHITECTURE,CODE_STYLE
REVIEW_EXECUTION_STRATEGY=sequential
REVIEW_CHECKPOINTS_ENABLED=true
LLM_BASE_URL=http://host.docker.internal:8080/v1
LLM_API_KEY=not-needed-for-local-llama-cpp
LLM_MODEL=fidelis-deepseek14b
LLM_TIMEOUT_SECONDS=300
LLM_MAX_TOKENS=4096
MAX_DIFF_CHARS=48000
MAX_SOURCE_CONTEXT_CHARS=12000
MAX_CONTEXT_FILES=6
SOURCE_CONTEXT_LINES=12
REVIEWER_TIMEOUT_SECONDS=1400
REVIEW_WORKER_CONCURRENCY=1
```

The endpoint must accept Chat Completions JSON and the `response_format: {"type":"json_object"}` option. The local default is **four specialist passes in sequence**. One model stays resident; requests are serial, not four models loaded in VRAM. A review can take several minutes on a 14B local model. The reviewer timeout is set to 1,400 seconds and local review-worker concurrency defaults to one.

For a cloud API, set its `LLM_BASE_URL`, `LLM_API_KEY` and `LLM_MODEL`, then select `REVIEW_EXECUTION_STRATEGY=single_pass`. That makes one LLM request containing the shared review packet and all selected specialist perspectives. `parallel` is available for a high-capacity inference endpoint. Specialist selection stays independent of scheduling: use `REVIEW_AGENTS` to choose any non-empty comma-separated set from `STATIC_ANALYSIS`, `SECURITY`, `ARCHITECTURE`, `CODE_STYLE` and `GENERAL`.

When `GITHUB_TOKEN` is set, the LLM reviewer also retrieves bounded source excerpts for changed files at the exact `commit_sha` being reviewed. It does not clone or send the whole repository. Context is limited by `MAX_SOURCE_CONTEXT_CHARS`, `MAX_CONTEXT_FILES` and `SOURCE_CONTEXT_LINES`. The patch remains authoritative. Model findings anchored to a line that is not actually added by the patch are rejected and counted; if every candidate is malformed or incorrectly anchored, the review fails visibly instead of being recorded as a clean review. Agent telemetry reports rejected candidates, context-file count and diff truncation.

No `GITHUB_TOKEN` is needed when a diff is provided directly to `POST /review`; source-file retrieval is skipped in that case. To attach current source excerpts to a supplied diff, or to fetch a diff automatically, configure a token. The token needs Pull requests: Read and Contents: Read on the target repository. Repository learning also needs permission to list and read merged PR files.

## GitHub integration

1. Expose the Gateway webhook route at `https://your-host/webhook/github` (a local tunnel is suitable for a demo).
2. Configure a GitHub webhook with the same `GITHUB_WEBHOOK_SECRET` value as `.env`; subscribe to `pull_request` events and use JSON delivery. The service accepts the planned `opened`, `synchronize` and `reopened` actions.
3. Configure `GITHUB_TOKEN` with Pull requests: Read and Contents: Read permissions for diff/source retrieval. For the consolidated PR report comment, grant **either** Issues: Read and write **or** Pull requests: Read and write on the exact target repository. A non-empty token is not proof of write access. See [GitHub delivery troubleshooting](docs/runbooks/github-delivery.md). Repository learning also needs permission to list merged PRs and read changed files. Prefer a narrowly scoped GitHub App installation token or fine-grained PAT.
4. The sample configuration enables `PUBLISH_GITHUB_REVIEWS=true`. After a successful review, Fidelis creates or updates one marked issue comment on the PR; subsequent commits update that same Fidelis comment rather than posting a new comment each time. Set `PUBLISH_GITHUB_REVIEWS=false` to disable delivery. Only enable this for repositories where the token is authorised to comment.

The GitHub report is a delivery side effect: if GitHub rejects a comment or rate-limits the API, the structured review remains completed and persisted; the delivery failure is appended to the audit trail. The primary report is one consolidated PR-level comment, not a flood of inline comments. The Control Room links directly to the published comment.

## Repository-specific learning

The learner scans recently merged PRs, changed file paths and added patch lines. It extracts simple, inspectable conventions (dominant file extension, primary source directory, test-file patterns, naming style and type-hint patterns), stores them under `(repository, rule_key)`, and includes them in future LLM prompt context. This is evidence-based convention extraction, not self-modifying model training. Start it from the Control Room or call the internal Learner API through the authenticated Gateway.

## Persistence and API surface

Important Orchestrator endpoints (internal service port 8001; Gateway proxy paths are prefixed with `/api/orchestrator/` and require a JWT):

| Endpoint | Purpose |
|---|---|
| `POST /jobs` | Idempotently create a job for `(repository, PR, commit SHA)` |
| `GET /jobs` / `GET /jobs/{id}` | List and inspect jobs |
| `GET /jobs/{id}/result` | Retrieve complete structured review result |
| `GET /jobs/{id}/events` | Lifecycle and GitHub delivery audit |
| `POST /jobs/{id}/replay` | Create a new review attempt without deleting prior history |
| `GET /repositories/{owner/repo}/rules` | Retrieve learned conventions |
| `PUT /repositories/{owner/repo}/rules` | Internal authenticated learner write |
| `GET /metrics/summary` | Dashboard metrics, agent runtime, slowest agent and token totals |
| `GET /metrics` | Prometheus text exposition |

The job row and dispatch-outbox row are committed together. The API makes an immediate dispatch attempt, and a background sweep retries pending outbox rows every five seconds when `ASYNC_REVIEW_ENABLED=true`. Results, findings, agent timings/tokens and audit events are committed by the Orchestrator; the worker does not write directly to PostgreSQL.

## Tests and experiments

Install the common development environment:

```bash
pip install -r requirements-dev.txt
```

Run service tests from their directories (the Orchestrator suite uses isolated SQLite fixtures):

```bash
cd services/orchestrator && DATABASE_URL=sqlite:///./test.db python -m pytest -q
cd ../reviewer && python -m pytest -q
cd ../gateway && python -m pytest -q
cd ../webhook && python -m pytest -q
cd ../learner && python -m pytest -q
```

Live-stack integration tests need Docker and a running compose stack:

```bash
docker compose up -d --build --wait
python -m pytest tests/integration -v
```

Compare sequential, single-pass and parallel execution strategies on the labelled diff fixtures (requires a reachable LLM endpoint):

```bash
python scripts/evaluate_pipelines.py --repeat 2
```

The harness records per-case expected/predicted categories, false positives, misses, precision/recall/F1, latency, model tokens and agent timing. It disables GitHub source retrieval because the benchmark uses synthetic fixture commit SHAs; use real PR jobs to evaluate source-context retrieval. The fixtures are simplified human-labelled examples, not a validated scientific benchmark; manually inspect every finding before using its numbers in a report.

Exercise concurrent Reviewer requests without creating GitHub or database jobs:

```bash
python scripts/load_test.py --requests 100 --concurrency 8
```

This can incur model charges when `REVIEWER_MODE=llm`. Start with low request counts and mock mode.

## Monitoring and Langfuse

Prometheus scrapes `http://orchestrator:8000/metrics`. Grafana is provisioned with the Fidelis overview dashboard. Orchestrator metrics include job states, finding volume, mean review/agent duration and aggregate input/output token counts.

Langfuse is disabled by default. To opt in, set `LANGFUSE_ENABLED=true`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` and `LANGFUSE_BASE_URL`. The integration uses the current Langfuse Python SDK v4 API. It emits per-agent generation observations and deliberately sends metadata/token counts, not source diffs or prompts containing code.

## Security and deployment notes

- Replace `JWT_SECRET`, `GITHUB_WEBHOOK_SECRET`, `WORKER_STATUS_TOKEN`, the Postgres password and Grafana password. Do not commit `.env`.
- The supplied compose file is a local development stack and publishes internal service/database ports to the host. For deployment, remove public bindings for Postgres, Redis, Orchestrator, Reviewer and Learner; publish only the reverse-proxied Gateway and observability endpoints you explicitly intend to expose.
- Set `ENV=production` to disable the Gateway development token and demo-injection endpoints. The current static Control Room uses the dev-token endpoint, so use a production-authenticated operator UI or disable the console in a production deployment.
- Restrict the internal worker token and do not route Reviewer/Learner internal APIs publicly.
- LLM output is untrusted. Findings are schema-validated, but code review output is advisory and must be checked by a developer. The deterministic scanner is a small high-signal complement, not a complete SAST engine.
- Never send secrets or unreviewed proprietary code to an external LLM or Langfuse workspace without approval.

## Known limits

2. Repository learning extracts simple path and code-pattern conventions. It does not infer deep architecture, train model weights, or guarantee that learned rules are correct.
3. GitHub delivery is best-effort. One marker-based issue comment is created or updated per PR; full structured findings and telemetry remain available in PostgreSQL and the Control Room.
4. There is no formal database migration framework yet. SQLAlchemy creates newly added tables and the Orchestrator repairs the active-job partial unique index on startup. For teams with non-disposable production data, introduce Alembic migrations before changing schemas in-place.
5. No live Docker/GitHub/LLM/Langfuse deployment can be certified from a unit-test-only environment. Run the compose smoke and integration tests on a Docker host with real test credentials before treating external integration as verified.

## Project documentation

- [Roadmap completion matrix](docs/completion-matrix.md)
- [Architecture and service ownership](docs/architecture.md)
- [Operations and troubleshooting](docs/runbooks/operations.md)
- [Evaluation protocol](docs/evaluation.md)
- [Security model](docs/security.md)
- [Reviewer contract](docs/contracts/reviewer-contract.md)
- [Review-worker contract](docs/contracts/review-worker-interface.md)
- [Webhook event contract](docs/contracts/webhook-events.md)
- [Week 3 P3 historical validation](docs/week3-p3-validation.md)
