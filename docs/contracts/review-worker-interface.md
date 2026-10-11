# Review Worker Interface (v1.0)

**Owner:** P1 · **Status:** stable for Weeks 3–4 · **Code:** `services/reviewer/app/worker/`

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

| Interface | Meaning | Reference implementations (in `app.worker`) | Real one (P3/P4) |
| --- | --- | --- | --- |
| `ReviewerClient` | "go and get this PR reviewed" | `HttpReviewerClient`, `InProcessReviewerClient` | `HttpReviewerClient` is production-ready |
| `JobStatusReporter` | "record what happened to this job" | `InMemoryStatusReporter`, `LoggingStatusReporter` | A database-backed reporter (below) |

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

The columns map straight onto the orchestrator's `ReviewJobDB`: `status`,
`started_at`, `completed_at`, `error_message`. Persisting the `ReviewResult` itself
(`job_completed`'s second argument) is P4's Week 4 work; the suggested tables are in
`reviewer-contract.md` §8. `InMemoryStatusReporter` is the executable specification:
a database-backed reporter must behave identically, including the audit trail it
keeps in `history`.

## 5. Wiring it into Celery (sketch for P3)

Illustrative only — Celery is not a dependency of the reviewer service, so this
exact code is not tested here:

```python
@celery_app.task(bind=True, name="orchestrator.tasks.run_review", max_retries=None)
def run_review(self, request_json: dict):
    attempt = self.request.retries + 1
    worker = ReviewWorker(
        HttpReviewerClient(base_url=REVIEWER_URL),
        DbStatusReporter(session_factory),     # P3/P4: implements JobStatusReporter
        max_attempts=MAX_ATTEMPTS,
    )
    outcome = worker.execute(ReviewRequest.model_validate(request_json), attempt=attempt)
    if outcome.disposition is ExecutionDisposition.RETRY:
        raise self.retry(countdown=outcome.retry_after_seconds)
    return outcome.model_dump(mode="json")
```

Build the request from a job row with
`ReviewRequest(job_id=job.id, repository=job.repository, pr_number=job.pr_number, commit_sha=job.commit_sha)`.
Set Celery's own `max_retries` high (or `None`) — **the worker owns the attempt
limit** — and use late acknowledgement (`acks_late=True`) if you want a task to be
redelivered after a worker crash; the `RUNNING → RUNNING` transition exists for
exactly that case.

### Open decision for P3 and P1 (settle before Week 4 wiring)

The orchestrator and the reviewer are separate Docker images and both use a
top-level package called `app`, so the orchestrator **cannot import**
`app.worker` from the reviewer's source. Two workable options:

1. **Run the Celery worker from the reviewer's image** — add `celery` to
   `services/reviewer/requirements.txt`, put the task next to `app.worker`, and give
   it a `JobStatusReporter` that calls the orchestrator (or the database).
2. **Copy the worker module** into the orchestrator service (it only depends on
   `pydantic`, `httpx` and the schema classes) and keep the tests with it.

Either way the interfaces and the test suite in `services/reviewer/tests/test_worker_*.py`
stay the specification. This is a team decision; nothing here forces one.

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
