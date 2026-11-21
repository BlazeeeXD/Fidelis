> **Historical snapshot:** This document records the original Week 3 / V2 implementation state. It predates V3–V9 work and contains statements about features that were not implemented at that time. It is retained for project history; use [the current README](../README.md), [completion matrix](completion-matrix.md), and [current contracts](contracts/) for the actual system behavior.

# Week 3 P3 test commands (Windows)

Run all commands from the repository root. Docker Desktop must be running.

## Upgrade in place from Week 2

```bat
docker compose down
```

Do **not** add `-v`. Extract the Week 3 ZIP into this same repository folder, overwrite source files, and preserve `.env`.

```bat
docker compose up --build -d
docker compose ps
```

Open `http://localhost:8000`.

## Local unit/API tests

```powershell
python -m pip install -r requirements-dev.txt
powershell -ExecutionPolicy Bypass -File scripts/test_week3.ps1
```

This runs the four service test suites. The Orchestrator suite uses SQLite for isolation. Redis/Celery runtime behavior is covered by the Docker-backed smoke test below.

## Week 3 end-to-end test (default mock succeeds)

```powershell
powershell -ExecutionPolicy Bypass -File scripts/smoke_week3.ps1
```

Expected: all checks pass and the summary ends with `0 failed`. The job should transition to `COMPLETED`.

## Retry test (optional)

Edit `.env`:

```dotenv
REVIEWER_MOCK_SCENARIO=flaky
REVIEWER_MOCK_FLAKY_FAILURES=2
```

Recreate the two services that read the Reviewer scenario:

```bat
docker compose up -d --force-recreate reviewer review-worker
```

Then run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/smoke_week3.ps1 -RequireRetry
```

Expected: the mock fails retryably twice, the Celery task retries, and the job eventually reaches `COMPLETED`.

## Permanent failure test (optional)

Set:

```dotenv
REVIEWER_MOCK_SCENARIO=fail_permanent
```

Recreate `reviewer` and `review-worker`, then run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/smoke_week3.ps1 -RequireFailure
```

Expected: the job reaches `FAILED`, `completed_at` and `error_message` are stored, and a new delivery may create a new job for the failed commit.

Restore the normal demo configuration afterwards:

```dotenv
REVIEWER_MOCK_SCENARIO=multi_agent
```

Then recreate `reviewer` and `review-worker` again.

## Week 2 regression

With the normal `multi_agent` mock restored:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/smoke_week2.ps1
```

This confirms the earlier Week 2 webhook, idempotency, database persistence, restart and outage checks still work.

## Useful diagnostics

```bat
docker compose ps
docker compose logs --tail=100 orchestrator
docker compose logs --tail=100 review-worker
docker compose logs --tail=100 redis
docker compose exec -T redis redis-cli ping
docker compose exec -T review-worker celery -A app.worker.celery_app:celery_app inspect ping --timeout=8
```

Never use `docker compose down -v` as a routine rebuild or troubleshooting step; it deletes the named database and Redis volumes.
