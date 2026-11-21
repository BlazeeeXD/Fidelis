# Review Worker Interface (v1.1)

**Owner:** Worker/Orchestrator · **Status:** implemented through V9 · **Code:** `services/reviewer/app/worker/`

This document defines how a **worker** (P3's Celery task) takes a queued review
job, gets it reviewed, and keeps the job's status truthful. It sits between two
other contracts:

* [`review-job-contract.md`](review-job-contract.md) — how a job is created and
  its fixed statuses (`QUEUED`, `RUNNING`, `COMPLETED`, `FAILED`).
* [`reviewer-contract.md`](reviewer-contract.md) — the `POST /review` request and
  result, and the error/retry rules.

```
Redis ──▶ Celery task ──▶ ReviewWorker.execute(request, attempt)
                              │ 1. reporter.job_started        (QUEUED  → RUNNING)
                              │ 2. client.review(request)      (HTTP → Reviewer Service)
                              │ 3. reporter.job_completed      (RUNNING → COMPLETED)
                              │    or job_retry_scheduled      (RUNNING → QUEUED)
                              │    or job_failed               (RUNNING → FAILED)
                              ▼
                        ExecutionOutcome  (what the task does next)
```

The worker logic knows nothing about Celery, Redis or SQL. Two small interfaces
are the seams, so all of it is testable without infrastructure:

| Interface | Meaning | Reference implementations (in `app.worker`) | Real one (Orchestrator) |
| --- | --- | --- | --- |
| `ReviewerClient` | "go and get this PR reviewed" | `HttpReviewerClient`, `InProcessReviewerClient` | `HttpReviewerClient` calls the Reviewer API |
| `JobStatusReporter` | "record what happened to this job" | `InMemoryStatusReporter`, `LoggingStatusReporter` | `HttpJobStatusReporter` calls the Orchestrator's authenticated status API; the Orchestrator commits the update to PostgreSQL |

## 1. Job status state machine

The statuses are the fixed set from `review-job-contract.md` — do not extend them
without team agreement (a test fails if the worker's `JobStatus` drifts from the
orchestrator's enum or from that document).

```
QUEUED ──▶ RUNNING ──▶ COMPLETED   (terminal)
   ▲          │
   └─(retry)──┤
              └──────▶ FAILED      (terminal)
```

| From | To | When |
| --- | --- | --- |
| `QUEUED` | `RUNNING` | A worker starts an attempt |
| `RUNNING` | `RUNNING` | **Crash recovery**: the queue redelivered the task because the previous worker died mid-attempt |
| `RUNNING` | `COMPLETED` | The reviewer returned a valid result |
| `RUNNING` | `QUEUED` | The attempt failed with a retryable error and attempts remain |
| `RUNNING` | `FAILED` | Non-retryable error, or retryable error on the last attempt |

`COMPLETED` and `FAILED` are terminal: starting a finished job raises
`InvalidTransitionError` *before* the reviewer is called, so a duplicate delivery
never produces a second review.

## 2. `ReviewWorker`

```python
worker = ReviewWorker(client, reporter, max_attempts=3,
                      backoff_base_seconds=2.0, backoff_cap_seconds=60.0)
outcome = worker.execute(request, attempt=1)   # one attempt
```

* One `execute` call is **one attempt** (`attempt` is 1-based, `1 ≤ attempt ≤ max_attempts`).
* **Reviewer failures never raise.** A `ReviewerError` — or any unexpected
  exception from the client, which is converted to a non-retryable
  `INTERNAL_ERROR` with a message capped at 500 characters — becomes an
  `ExecutionOutcome`. The job status is updated first, so it is never left
  `RUNNING` by an error the worker understood.
* **Reporter failures do raise** (for example the database is down). The worker
  cannot record status, so it must not pretend it did. Let the task runner's own
  retry deal with it; the job keeps whatever status it last had (`QUEUED` if the
  failure was on start, `RUNNING` if it came later) and a redelivery resumes it.
* A result for a *different* job (`job_id`, `repository`, `pr_number` or
  `commit_sha` differs from the request) is rejected as a non-retryable
  `INVALID_RESPONSE`.
* Retry rule (from `reviewer-contract.md` §5): retry only if the error is
  `retryable` **and** `attempt < max_attempts`. Delay after failed attempt *n* is
  `min(cap, base × 2^(n-1))` → **2 s, 4 s, 8 s, …, capped at 60 s** by default.

### `ExecutionOutcome`

JSON-serialisable (`outcome.model_dump(mode="json")`), so it can be a Celery
result. The shape is validated, so a field combination that contradicts the
disposition cannot be constructed.

| `disposition` | `job_status` | `result` | `error` | `retry_after_seconds` | What the task does |
| --- | --- | --- | --- | --- | --- |
| `SUCCEEDED` | `COMPLETED` | set | – | – | Return; done |
| `RETRY` | `QUEUED` | – | set | set | Re-run the task after that delay with `attempt + 1` |
| `FAILED` | `FAILED` | – | set | – | Return; do not retry |

Other fields: `job_id`, `attempt`, `max_attempts`, `duration_ms` (wall time of this
attempt).

## 3. `ReviewerClient`

```python
class ReviewerClient(ABC):
    def review(self, request: ReviewRequest) -> ReviewResult: ...   # raises ReviewerError
```

Synchronous on purpose (Celery tasks are plain functions). Whatever the transport,
every failure surfaces as a `ReviewerError` with a code and a `retryable` flag —
never a raw `httpx` exception.

`HttpReviewerClient(base_url="http://reviewer:8000", timeout=120.0)` maps
responses as follows:

| Situation | Raised as | Retryable |
| --- | --- | --- |
| Connection refused, DNS, timeout | `REVIEWER_UNAVAILABLE` | yes |
| Non-2xx with an `ErrorResponse` body | that error, verbatim | as the body says |
| Non-2xx without one: `408`, `429`, `5xx` | `REVIEWER_UNAVAILABLE` | yes |
| Non-2xx without one: other `4xx` | `INVALID_REQUEST` | no |
| `2xx` that does not match the contract | `INVALID_RESPONSE` | no |

The default timeout is generous because a real LLM review can take a while.

## 4. `JobStatusReporter` — what P3 and P4 implement

| Method | Transition | Must also |
| --- | --- | --- |
| `job_started(job_id, *, attempt)` | `QUEUED → RUNNING` (or `RUNNING → RUNNING`) | set `started_at` on the **first** start only; keep it across retries |
| `job_completed(job_id, result)` | `RUNNING → COMPLETED` | set `completed_at`; clear `error_message`; persist `result` (see below) |
| `job_retry_scheduled(job_id, *, attempt, error, retry_after_seconds)` | `RUNNING → QUEUED` | store `error.message` in `error_message` as a note |
| `job_failed(job_id, *, attempt, error)` | `RUNNING → FAILED` | set `completed_at`; store `error.message` in `error_message` |

Rules for every implementation:

* Raise `InvalidTransitionError` when the job's current status does not allow the
  move (`ALLOWED_TRANSITIONS` in `app.worker.interface` is the table), and
  `UnknownJobError` for an id it has never seen.
* Open its **own database session** and **commit before returning** — it runs in
  a worker process, not inside a web request.
* Do not swallow errors: if the write fails, raise.

The lifecycle columns map onto the Orchestrator's `ReviewJobDB`. On completion,
`HttpJobStatusReporter` sends the full `ReviewResult` in the same callback. The
Orchestrator commits the terminal status, canonical result JSON, normalized findings,
per-agent timings/token usage and audit event in one database transaction.
`InMemoryStatusReporter` is the executable specification:
a database-backed reporter must behave identically, including the audit trail it
keeps in `history`.

## 5. Week 3 Celery wiring (P3 implementation)

The implementation chooses **Option 1** below: the Celery worker runs from the
Reviewer image as a separate `review-worker` container. It imports this
`ReviewWorker`, uses `HttpReviewerClient` to call `http://reviewer:8000`, and
uses `HttpJobStatusReporter` to call the Orchestrator's authenticated
`POST /jobs/{job_id}/status` endpoint. The Orchestrator owns PostgreSQL writes.

The Orchestrator publishes task `fidelis.run_review` to the `review-jobs` queue
using Redis as the Celery broker. The task receives a JSON `ReviewRequest`,
executes one attempt, and calls Celery retry with the exact backoff chosen by
`ReviewWorker` when the outcome is `RETRY`. The review attempt counter is passed
separately from infrastructure-reporting retries.

Celery is configured for JSON messages, late acknowledgement, worker-lost
redelivery, and a prefetch multiplier of one. This is **at-least-once** delivery;
it is not a claim of exactly-once execution. Terminal job states reject another
start, and `RUNNING -> RUNNING` is allowed for crash-recovery redelivery. A
future reliability pass can add a lease/claim token to distinguish stale
redelivery from two concurrent deliveries for the same job.

`HttpJobStatusReporter.job_completed()` reports the terminal status and the
complete structured result. The Orchestrator persists result/finding/agent telemetry
atomically; Redis is not the system of record. Celery's result backend retains the
serialised `ExecutionOutcome` for task inspection only.

The main pieces are:

- `services/reviewer/app/worker/celery_app.py` — Celery app/configuration.
- `services/reviewer/app/worker/tasks.py` — `fidelis.run_review` task adapter.
- `services/reviewer/app/worker/reporters.py` — authenticated HTTP status reporter.
- `services/orchestrator/app/services/queue_dispatch.py` — outbox publisher.
- `services/orchestrator/app/api/jobs.py` — job creation dispatch and worker status endpoint.

The Orchestrator also writes a `review_job_outbox` row in the same transaction
as each newly created job. A pending row remains available for a later webhook redelivery if Redis publication
fails. When async review is enabled, a periodic Orchestrator outbox sweeper also
retries pending rows every five seconds, so recovery does not depend on a new event.

### Alternative placement (not selected)

The worker module could instead be copied into the Orchestrator service, but
that duplicates P1's worker contract and risks drift between the two top-level
`app` packages. The current implementation keeps the worker execution logic
with the Reviewer and talks to the Orchestrator over HTTP.

## 6. Mock execution

`app.worker.mock_execution.run_mock_execution()` runs a whole job lifecycle with
the mock reviewer and an in-memory reporter — no Redis, Celery, database or
network — and returns the outcomes, the status history and the final job record.
Backoff delays are skipped.

```
cd services/reviewer
python -m app.worker.demo                          # happy path → COMPLETED
python -m app.worker.demo --scenario flaky         # fails twice, then COMPLETED
python -m app.worker.demo --scenario fail_permanent   # FAILED on attempt 1
python -m app.worker.demo --scenario timeout --max-attempts 2
```

Example (`--scenario flaky`):

```
job mock-job-1  scenario=flaky  max_attempts=3
  attempt 1/3: RETRY [UPSTREAM_ERROR: ...] -> retry in 2s
  attempt 2/3: RETRY [UPSTREAM_ERROR: ...] -> retry in 4s
  attempt 3/3: SUCCEEDED (6 finding(s), highest=HIGH)
  status path: QUEUED -> RUNNING -> QUEUED -> RUNNING -> QUEUED -> RUNNING -> COMPLETED
  final status: COMPLETED
```

The command exits `0` when the job ends `COMPLETED` and `1` otherwise, so it also
works as a smoke check. The available scenarios are the ones in
`reviewer-contract.md` §6.

## 7. Changelog

| Version | Change |
| --- | --- |
| 1.0 | Initial worker interface and mock execution (Week 3) |
