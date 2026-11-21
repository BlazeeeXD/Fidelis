> **Historical snapshot:** This document records the original Week 3 / V2 implementation state. It predates V3–V9 work and contains statements about features that were not implemented at that time. It is retained for project history; use [the current README](../README.md), [completion matrix](completion-matrix.md), and [current contracts](contracts/) for the actual system behavior.

# Fidelis — Week 3 P3 implementation guide

## Scope from the team build plan

Week 3 is V2: **PR → queue → worker**. P3 owns Redis/Celery integration, worker lifecycle and job states. P1 owns the reviewer/worker execution contract and mock execution. P4 owns the database schema and persistence of lifecycle state. P5 owns failure/retry and Docker integration tests.

This implementation reuses P1's existing `ReviewWorker`, `ReviewerClient`, `JobStatusReporter` and retry policy. It does not introduce an AI model. The Reviewer still runs the configured deterministic mock (`REVIEWER_MODE=mock`) until the later AI-review milestone.

## What was added

1. **Redis service** (`redis:7-alpine`): Celery broker on database 0 and result backend on database 1.
2. **Celery producer in the Orchestrator**: when async dispatch is enabled, a newly created or still-queued job is published to queue `review-jobs` as task `fidelis.run_review`.
3. **Transactional dispatch outbox**: a `review_job_outbox` row is committed in the same database transaction as the `review_jobs` row. The publisher locks the outbox row, publishes the task and records `published_at`. If broker publication fails, the outbox row remains pending and a webhook redelivery can retry it.
4. **Dedicated `review-worker` container**: runs Celery from the Reviewer image. It consumes the task, calls the Reviewer HTTP API, and uses P1's `ReviewWorker` for attempt-level logic.
5. **Authenticated lifecycle callback**: the worker reports `started`, `retry_scheduled`, `completed` and `failed` events to `POST /jobs/{job_id}/status`. The Orchestrator validates transitions and commits the current job fields in PostgreSQL.
6. **Retry policy**: P1's worker policy is retained: 3 review attempts by default, exponential backoff of 2s, 4s, then terminal failure. Celery late acknowledgement and worker-lost redelivery are enabled.
7. **Week 3 smoke test**: `scripts/smoke_week3.ps1` verifies Redis, the Celery worker, asynchronous completion, persisted timestamps, duplicate behavior and a new-commit execution.

## Architecture choice

The worker process runs from the **Reviewer image**, alongside (but in a separate container from) the Reviewer API. This avoids importing one service's top-level `app` package into the Orchestrator image. The worker calls the Reviewer through HTTP and reports state changes to the Orchestrator through its authenticated internal API. It does not connect to PostgreSQL directly.

```text
Webhook → Orchestrator → PostgreSQL
                 │
                 └─ outbox → Redis → Celery review-worker
                                           │
                                           ├─ HTTP → Reviewer API
                                           └─ HTTP → Orchestrator status API → PostgreSQL
```

## Job lifecycle

```text
QUEUED → RUNNING → COMPLETED
   ▲        │
   └ retry ┘
            └────────→ FAILED
```

- `started`: `QUEUED → RUNNING`; `started_at` is set once and retained over retries.
- `retry_scheduled`: `RUNNING → QUEUED`; `error_message` records the retry reason.
- `completed`: `RUNNING → COMPLETED`; `completed_at` is set and `error_message` cleared.
- `failed`: `RUNNING → FAILED`; `completed_at` is set and the final error is recorded.
- `RUNNING → RUNNING` is allowed for late-ack crash redelivery, as specified by P1's worker contract.
- `COMPLETED` and `FAILED` are terminal states.

The current `ReviewJobDB` has the fields required for these transitions. This implementation does **not** persist the `ReviewResult`/findings yet; that is P4's Week 4 work. The validated task outcome is stored temporarily in Celery's Redis result backend.

## Run it on Windows

If the Week 2 stack is still running, run this from the **same repository
folder** before replacing files:

```bat
docker compose down
```

Do not add `-v`: that would remove the named PostgreSQL volume. Extract the
Week 3 ZIP over the same repository folder and preserve your existing `.env`
file. If you do not have a `.env`, create it with `copy .env.example .env`
from Command Prompt (or `Copy-Item .env.example .env` in PowerShell). When
extracting the ZIP, choose the existing repository folder as the destination
and allow source files to be overwritten; the archive deliberately does not
contain `.env`.

Then, from the repository root:

```bat
docker compose up --build -d
docker compose ps
```

The Compose project name is derived from the folder name by default. Keep using
the same folder for upgrades; running two copies from differently named folders
can create separate stacks that compete for host ports 8000–8003 and 5432.
If you previously used another folder name and an old stack still owns a port,
inspect it with `docker ps --format "table {{.Names}}\t{{.Ports}}"`; stop that
old Compose project or remove only the stale container. Do not delete database
volumes as a port-conflict fix. Also ensure `ASYNC_REVIEW_ENABLED=true` in `.env`
for the Week 3 run; set `REVIEWER_MOCK_SCENARIO=multi_agent` for the default
successful mock scenario.

Expected running services: `redis`, `postgres`, `orchestrator`, `reviewer`, `review-worker`, `webhook`, `gateway`.

Open the Control Room at `http://localhost:8000`. Send a sample PR event. In Week 2 it stayed `QUEUED`; in Week 3 the worker should move it to `RUNNING` and then `COMPLETED` (using the default mock reviewer).

## Automated verification

Run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/smoke_week3.ps1
```

For explicit retry-path verification:

1. Set `REVIEWER_MOCK_SCENARIO=flaky` in `.env` (the mock fails retryably twice, then succeeds).
2. Recreate the API and worker so they load the updated reviewer configuration:

   ```bat
   docker compose up -d --force-recreate reviewer review-worker
   ```
3. Run:

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts/smoke_week3.ps1 -RequireRetry
   ```

The smoke script expects the job to finish `COMPLETED` and checks worker logs for retry messages. Run the optional scenarios one at a time; do not set both switches in the same run.

For a permanent-failure test, set `REVIEWER_MOCK_SCENARIO=fail_permanent` in `.env`,
recreate `reviewer` and `review-worker`, then run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/smoke_week3.ps1 -RequireFailure
```

The job should end in `FAILED`, with `completed_at` and `error_message` populated. The script also verifies that a new delivery can create a fresh job for that previously failed commit (failed jobs are deliberately excluded from the active-job uniqueness rule).

## Configuration

| Variable | Default in Compose | Purpose |
|---|---|---|
| `ASYNC_REVIEW_ENABLED` | `true` | Enables queue publication from the Orchestrator. Set `false` to run the Week 2 intake-only mode. |
| `CELERY_BROKER_URL` | `redis://redis:6379/0` | Redis broker address (internal Docker network). |
| `CELERY_RESULT_BACKEND` | `redis://redis:6379/1` | Redis result backend used by the worker. |
| `WORKER_STATUS_TOKEN` | local development token | Authenticates worker-to-Orchestrator lifecycle callbacks. Set a private value in `.env` outside local demos. |
| `REVIEW_MAX_ATTEMPTS` | `3` | Maximum reviewer attempts per job. |
| `REVIEWER_TIMEOUT_SECONDS` | `120` | HTTP timeout when calling the Reviewer API. |
| `REVIEWER_MOCK_SCENARIO` | `multi_agent` | Deterministic mock behavior. Use `flaky` to exercise retries. |

## Important engineering notes / limitations

- Celery delivery is **at-least-once**, not a promise of mathematically exactly-once execution. The outbox preserves pending dispatch intent and the job state machine blocks re-execution after terminal states. The `RUNNING → RUNNING` transition intentionally supports crash recovery; a future reliability milestone can add an execution lease/claim token to distinguish a stale worker from two truly concurrent duplicate deliveries.
- The webhook's delivery-ID cache remains in memory and is not durable across Webhook restarts. The Orchestrator's database-backed job deduplication remains the second layer.
- Queue publication failure leaves the job `QUEUED` and the outbox pending. A GitHub/webhook redelivery or repeat POST for the same job can retry publication.
- Result/finding persistence, real AI review, GitHub comments, multi-agent aggregation and repository learning are outside Week 3 P3 scope.
- `WORKER_STATUS_TOKEN` is a local development default in Compose. Change it in `.env` for any shared or deployed environment.

## Automated checks

Run the local unit/API suites (including the Celery adapter tests):

```powershell
python -m pip install -r requirements-dev.txt
powershell -ExecutionPolicy Bypass -File scripts/test_week3.ps1
```

Run the Docker-backed Week 3 integration test:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/smoke_week3.ps1
```

Run the Week 2 regression test as well:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/smoke_week2.ps1
```

Optional retry scenario: set `REVIEWER_MOCK_SCENARIO=flaky` in `.env`, recreate
`reviewer` and `review-worker`, then run `scripts/smoke_week3.ps1 -RequireRetry`.
Optional terminal-failure scenario: set `REVIEWER_MOCK_SCENARIO=fail_permanent`,
recreate those two services, then run `scripts/smoke_week3.ps1 -RequireFailure`.
Restore `REVIEWER_MOCK_SCENARIO=multi_agent` afterward and recreate those two
services.

## P3 definition of done

- [ ] Redis starts and passes `redis-cli ping`.
- [ ] Celery worker answers `inspect ping` and registers `fidelis.run_review`.
- [ ] A new PR event creates one PostgreSQL job and a matching outbox record.
- [ ] The outbox dispatches the task to Redis/Celery and records `published_at`.
- [ ] Worker calls the mock Reviewer over HTTP (not an in-process shortcut).
- [ ] Worker transitions the job `QUEUED → RUNNING → COMPLETED`.
- [ ] `started_at` and `completed_at` are persisted by the Orchestrator in PostgreSQL.
- [ ] Worker status callbacks require the internal worker token.
- [ ] Completed/failed jobs reject illegal re-entry.
- [ ] Retryable Reviewer failure returns the job to `QUEUED` and Celery retries within the configured attempt budget.
- [ ] Permanent/exhausted failure ends in `FAILED` with an error message.
- [ ] Duplicate delivery and duplicate commit do not create a second active review job.
- [ ] Week 2 webhook, API validation, persistence, restart and outage checks still pass.

## Scope boundaries

P3 owns the Redis/Celery dispatch and worker orchestration. P1's `ReviewWorker`
contract and deterministic mock are reused rather than replaced. The Orchestrator
remains the sole writer of the PostgreSQL job row; P4 should review the status
schema and persistence boundary with P3. Week 3 does not add real AI, findings
persistence, multi-agent aggregation or GitHub review comments.
