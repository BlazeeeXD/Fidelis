# Reviewer Contract (v1.0)

**Owner:** P1 · **Status:** stable for Weeks 1–3 · **Code:** `services/reviewer/app/schemas/`

This document defines what goes **into** and comes **out of** the Reviewer
Service. The same contract is served by the mock reviewer today and by the real
LangGraph reviewer from Week 4, so everything built against it keeps working.

It extends [`review-job-contract.md`](review-job-contract.md) (webhook →
orchestrator → database). The Celery worker side — how a worker runs one review
and reports job status — is specified in `review-worker-interface.md` (added in Week 3).

```
GitHub PR ──▶ Webhook ──▶ Orchestrator ──▶ Redis ──▶ Celery worker ──▶ Reviewer Service
              (P2)         (P3, job row: P4)  (P3)        (P3)         POST /review  (P1)
                                                                              │
                                              ReviewResult  ◀─────────────────┘
```

Machine-readable versions live next to this file:

| What | Where |
| --- | --- |
| JSON Schemas | [`schemas/review-request.schema.json`](schemas/review-request.schema.json), [`schemas/review-result.schema.json`](schemas/review-result.schema.json), [`schemas/error-response.schema.json`](schemas/error-response.schema.json) |
| Example payloads (copy into your tests) | [`examples/`](examples/) |

Both are **generated from the code** (`python -m app.contract_export` in
`services/reviewer`) and a test fails if they go stale — if the docs and the code
disagree, the code is right.

## 1. Versioning

* `schema_version` is `"1.0"` and is present on every request and result.
* Every model **rejects unknown fields**, so a typo or an un-agreed addition fails
  loudly at the boundary instead of being silently dropped.
* Adding a field, an enum value, or tightening a rule is a **contract change**:
  bump the version, regenerate the artifacts, update this file, and tell the team
  (same rule as the job statuses in `review-job-contract.md`).

## 2. Endpoints

| Method & path | Purpose | Success | Notes |
| --- | --- | --- | --- |
| `POST /review` | Review one PR commit | `200` + `ReviewResult` | Synchronous; may take a while with a real LLM — use a generous client timeout |
| `GET /healthz` | Liveness (compose healthcheck) | `200 {"status":"ok"}` | |
| `GET /info` | Which reviewer is active | `200 {"name","version","mode"}` | `mode` is `MOCK` or `LLM` |

The reviewer is an **internal** service: it is not routed through the gateway.
Internally it is `http://reviewer:8000`; from the host it is `localhost:8003`.

## 3. Request — `POST /review`

| Field | Type | Required | Rules |
| --- | --- | --- | --- |
| `schema_version` | `"1.0"` | no (default `"1.0"`) | any other value is rejected |
| `job_id` | string | yes | 1–64 chars; the orchestrator's `ReviewJob.id`; echoed back in the result |
| `repository` | string | yes | `owner/repo`, characters `A-Z a-z 0-9 _ . -` |
| `pr_number` | integer | yes | `> 0` |
| `commit_sha` | string | yes | 1–64 chars (same loose rule the orchestrator uses) |
| `diff` | string \| null | no | Unified diff. **Normally omitted**: the real reviewer fetches the diff from GitHub. If supplied it is used as-is (handy for tests, and keeps mock runs offline). Max 2,000,000 chars |

```json
{
  "job_id": "5f0d3c1e-8a52-4a8e-9b8e-3c9a6d1f2b77",
  "repository": "acme/payments-api",
  "pr_number": 42,
  "commit_sha": "9fceb02d0ae598e95dc970b74767f19372d61af8"
}
```

## 4. Response — `ReviewResult`

| Field | Type | Meaning |
| --- | --- | --- |
| `schema_version` | `"1.0"` | |
| `job_id`, `repository`, `pr_number`, `commit_sha` | | Echo of the request |
| `reviewer` | `{name, version, mode}` | Which implementation produced this |
| `agents_run` | `ReviewAgent[]` | Agents that ran, in execution order |
| `findings` | `Finding[]` | **Sorted by priority** (see below). Empty list = clean PR |
| `summary` | `ReviewSummary` | Counts, always consistent with `findings` |
| `started_at`, `completed_at` | ISO-8601 UTC | Timezone-aware |
| `duration_ms` | integer | `completed_at − started_at` |

### `Finding`

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `id` | string | yes | Unique id (UUID string). Stable once created |
| `agent` | `ReviewAgent` | yes | Which agent raised it |
| `severity` | `Severity` | yes | `HIGH` \| `MEDIUM` \| `LOW` |
| `category` | `Category` | yes | See enums below |
| `file` | string \| null | yes | Repo-relative path, `/` separators; `null` for PR-level findings |
| `line` | integer \| null | yes | 1-based line in the **new** version of the file; `null` if no line |
| `end_line` | integer \| null | no | Last line of a multi-line finding (inclusive) |
| `message` | string | yes | What is wrong (1–4000 chars, never blank) |
| `suggestion` | string \| null | no | Concrete fix when one is known |
| `confidence` | number | no | `0.0`–`1.0`, default `1.0` |

Location rules (all enforced): `line` requires `file`; `end_line` requires `line`
and must be `>= line`; `file` must not be absolute, contain `..`, or use `\`.
`file`/`line` are the five core fields from the project plan
(`severity`, `category`, `file`, `line`, `message`) plus the extras above.

**Ordering.** `findings` is sorted by severity (HIGH → LOW), then findings that
have a file before PR-level ones, then `file`, `line`, `agent`, `id`. P2 can post
them in this order; P4 can store them in this order.

### `ReviewSummary`

`total_findings`, `by_severity`, `by_category`, `by_agent` and `highest_severity`
(`null` when there are no findings). **Every enum value appears as a key, with
`0` when absent**, so metrics code (P5) never needs to handle missing keys.
Parsing a result whose summary or ordering disagrees with its findings is an
error, so a corrupted payload is rejected rather than trusted.

### Enums (closed sets)

| Enum | Values |
| --- | --- |
| `Severity` | `HIGH`, `MEDIUM`, `LOW` |
| `Category` | `BUG`, `SECURITY`, `ARCHITECTURE`, `MAINTAINABILITY`, `STYLE` |
| `ReviewAgent` | `STATIC_ANALYSIS`, `SECURITY`, `ARCHITECTURE`, `CODE_STYLE`, `GENERAL` |
| `ReviewerMode` | `MOCK`, `LLM` |

`GENERAL` is the single all-purpose reviewer of the V3 milestone, before the
four-agent split. `MAINTAINABILITY` covers duplication and maintainability from
the synopsis.

## 5. Errors

Every non-2xx response has the body `{"error": {"code", "message", "retryable", "details"}}`
— including request-validation failures (FastAPI's default `{"detail": ...}` shape
is **not** used).

| `code` | HTTP | `retryable` | When |
| --- | --- | --- | --- |
| `INVALID_REQUEST` | 422 | no | Body does not match the contract; `details.errors` lists each problem |
| `UPSTREAM_ERROR` | 502 | **yes** | GitHub or the LLM provider failed |
| `REVIEWER_UNAVAILABLE` | 503 | **yes** | Reviewer overloaded or shutting down |
| `REVIEW_TIMEOUT` | 504 | **yes** | The review exceeded its time budget |
| `INTERNAL_ERROR` | 500 | no | Unexpected bug; the message is deliberately generic, details go to the logs |
| `INVALID_RESPONSE` | — | no | **Client-side only**: a 2xx body that does not match this contract |

**Caller rule:** retry only when `retryable` is `true` (with backoff, up to the
worker's attempt limit); otherwise mark the job `FAILED` and store
`error.message` in `ReviewJob.error_message`. A transport failure (connection
refused, timeout) is treated as `REVIEWER_UNAVAILABLE`.

## 6. Mock reviewer

Until Week 4 the service runs `MockReviewer`: no network, no API keys, fully
deterministic (the same request always yields the same finding ids, files and
lines). Pick its behaviour with `REVIEWER_MOCK_SCENARIO` (restart the container
after changing it):

| Scenario | Result |
| --- | --- |
| `multi_agent` *(default)* | 6 findings from all four agents, mixed severities/categories, one PR-level |
| `single_bug` | The one example from the plan: `HIGH` / `BUG` / `foo.py` / `42` |
| `clean` | No findings (the "clean PR" case) |
| `fail_retryable` | Always `UPSTREAM_ERROR` (502, retryable) |
| `timeout` | Always `REVIEW_TIMEOUT` (504, retryable) |
| `fail_permanent` | Always `INTERNAL_ERROR` (500, not retryable) |
| `flaky` | Fails retryably `REVIEWER_MOCK_FLAKY_FAILURES` times (default 2) **per `job_id`**, then succeeds — for retry testing |

`REVIEWER_MOCK_DELAY_MS` adds artificial latency to every review (to see a job
sit in `RUNNING`). If the request includes a `diff`, located findings are
anchored on real added lines of that diff, so inline-comment posting can be
tested against real PRs.

## 7. Field names across services

The services spell the same PR identifiers differently, so each hand-off maps
them. The webhook already does its half (`repo_full_name` → `repository`,
`head_sha` → `commit_sha` when it calls the orchestrator's `POST /jobs`); the
worker does the other half when it builds a `ReviewRequest` from a job row:

| Concept | Webhook `ReviewJobMessage` | Orchestrator `POST /jobs` / `ReviewJob` | Reviewer `ReviewRequest` |
| --- | --- | --- | --- |
| Repository | `repo_full_name` | `repository` | `repository` |
| PR number | `pr_number` | `pr_number` | `pr_number` |
| Commit | `head_sha` | `commit_sha` | `commit_sha` |
| Job id | `event_id` (GitHub delivery id — **not** a job id) | `id` / `job_id` | `job_id` |

## 8. Notes for teammates

* **P2 (GitHub):** `findings` come pre-sorted and carry `file`/`line`/`end_line`
  for inline comments; `file = null` findings belong in the summary comment. From
  Week 4 the reviewer will either fetch the diff itself or accept the one you
  pass in `diff` — agree which with P1.
* **P3 (queue/worker):** call `POST /review` with the request above, apply the
  retry rule in section 5, and report job state (`QUEUED` → `RUNNING` →
  `COMPLETED`/`FAILED`) as specified in `review-worker-interface.md` (Week 3).
  `POST /jobs` is idempotent on `(repository, pr_number, commit_sha)`, so a webhook
  redelivery returns the *same* `job_id`; a result is always tied to that `job_id`,
  and a new commit on the PR (or a retry after `FAILED`) gets a new one.
* **P4 (database):** suggested persistence for results — a `review_results` row
  per job (`job_id`, `schema_version`, `reviewer_name/version/mode`,
  `started_at`, `completed_at`, `duration_ms`) and a `review_findings` row per
  finding (`id` String(36) PK, `job_id` FK, `agent`, `severity`, `category`,
  `file` nullable, `line`/`end_line` nullable, `message` Text, `suggestion` Text
  nullable, `confidence` Float). Don't store the summary counts — they are a
  `GROUP BY` away. Use string-backed enums (`Enum(..., native_enum=False)`) like
  `ReviewJobDB.status`. This is a recommendation; the schema is yours.
* **P5 (DevOps/QA):** use the files in `examples/` for contract tests, and the
  scenarios above to test failure and retry paths in the compose stack.

## 9. Changelog

| Version | Change |
| --- | --- |
| 1.0 | Initial contract (Weeks 1–3) |
