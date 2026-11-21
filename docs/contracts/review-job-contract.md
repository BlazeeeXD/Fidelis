# Review job contract (current)

This contract defines the durable job record and its lifecycle, from signed GitHub delivery through asynchronous review execution.

## Create job

`POST /jobs` accepts:

```json
{
  "repository": "owner/repo",
  "pr_number": 42,
  "commit_sha": "full-or-short-commit-sha"
}
```

`repository` must match `owner/repo`; `pr_number` must be positive; `commit_sha` must contain 1–64 characters.

The response is `JobCreateResponse`:

```json
{
  "job_id": "uuid",
  "status": "QUEUED",
  "duplicate": false
}
```

Creation is idempotent on `(repository, pr_number, commit_sha)`. A duplicate delivery returns the newest existing non-FAILED job with `duplicate: true`. A failed job does not block another attempt. Completed jobs remain idempotent; an explicit replay is required to create a fresh attempt after completion.

The database has a partial unique index for active `(repository, pr_number, commit_sha)` jobs while `status` is `QUEUED` or `RUNNING`. This is the cross-process guard for concurrent creation. Completed/failed attempts remain in history.

## Transactional dispatch outbox

The Orchestrator inserts `review_jobs` and `review_job_outbox` in the same database transaction. After commit, the API attempts to publish `fidelis.run_review` to the Redis `review-jobs` queue. It marks the outbox row as published only after the publish call succeeds.

If Redis is unavailable, the job and pending outbox record remain durable. When `ASYNC_REVIEW_ENABLED=true`, the Orchestrator lifespan starts a sweep every five seconds and retries pending rows in bounded batches. Delivery is at-least-once, not exactly-once.

## Status lifecycle

Allowed states (closed enum):

QUEUED, RUNNING, COMPLETED, FAILED

```text
QUEUED ──► RUNNING ──► COMPLETED
   ▲           │
   └─ retry ───┤
               └─────► FAILED
```

- `started`: `QUEUED → RUNNING`; the first `started_at` is retained across retries.
- `retry_scheduled`: `RUNNING → QUEUED`; `error_message`, `attempt` and `retry_after_seconds` are recorded in the audit trail.
- `completed`: `RUNNING → COMPLETED`; the full structured `ReviewResult` is required and is stored atomically with normalized findings, agent telemetry and the state transition.
- `failed`: `RUNNING → FAILED`; the final error is recorded.
- `RUNNING → RUNNING` is allowed for Celery late-ack crash redelivery.
- `COMPLETED` and `FAILED` are terminal for an individual job attempt.

Worker status callbacks require `X-Worker-Token`. A completed callback without a structured result is rejected.

## Durable review history

| Table | Responsibility |
|---|---|
| `review_jobs` | One row for each review attempt and its lifecycle state |
| `review_job_outbox` | Pending/published dispatch intent |
| `review_results` | Canonical versioned result payload and reviewer/timing metadata |
| `review_findings` | Queryable finding rows by job, agent, severity and category |
| `review_agent_runs` | Per-agent execution duration, findings count, token usage and model |
| `review_audit_events` | State transitions, retry metadata, replay lineage and GitHub delivery outcomes |
| `repository_rules` | Explainable repository-specific conventions |

The Reviewer worker does not write to PostgreSQL. The Orchestrator is the only owner of job and review-result persistence.

## API endpoints

| Method / path | Purpose |
|---|---|
| `POST /jobs` | Create or retrieve an idempotent review job |
| `GET /jobs?repository=&pr_number=&status=&limit=` | List jobs, newest first |
| `GET /jobs/{job_id}` | Read job state and timestamps |
| `POST /jobs/{job_id}/status` | Internal worker status/result callback |
| `GET /jobs/{job_id}/result` | Retrieve the complete structured result |
| `GET /jobs/{job_id}/events` | Retrieve lifecycle and delivery audit events |
| `POST /jobs/{job_id}/replay` | Create a new queued attempt without deleting previous history |
| `POST /jobs/{job_id}/delivery` | Internal GitHub delivery audit callback |
| `GET /repositories/{owner/repo}/rules` | Read stored repository conventions |
| `PUT /repositories/{owner/repo}/rules` | Internal learner update |
| `GET /metrics/summary` | Dashboard metrics |
| `GET /metrics` | Prometheus text exposition |

## Webhook mapping

The Webhook service verifies `X-Hub-Signature-256` against the raw body using `GITHUB_WEBHOOK_SECRET`, filters supported `pull_request` actions, and maps:

| GitHub payload | Job field |
|---|---|
| `repository.full_name` | `repository` |
| `number` | `pr_number` |
| `pull_request.head.sha` | `commit_sha` |
| `X-GitHub-Delivery` | Webhook delivery deduplication key (not `job_id`) |

The accepted trigger actions are `opened`, `synchronize` and `reopened`. Duplicate delivery IDs are ignored. A distinct delivery for the same repo/PR/commit reuses the existing non-FAILED job.

## Replay semantics

`POST /jobs/{job_id}/replay` creates a new job attempt with a new UUID and new outbox row. The original job, findings and audit events remain unchanged. If an active job for the same repo/PR/commit already exists, the existing active job is returned.

## Health and operations

- `GET /health` returns `200 {"status":"healthy"}` from the Orchestrator.
- `GET /metrics/summary` returns totals, severity/category/agent breakdown, average job duration, per-agent duration, slowest agent and token aggregates.
- `GET /metrics` is scraped by Prometheus.

The browser-facing Gateway proxies Orchestrator endpoints under `/api/orchestrator/` and requires a JWT. Internal status and delivery callbacks must use the worker token.
