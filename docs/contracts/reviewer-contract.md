# Reviewer Contract (v1.3)

**Owner:** Reviewer service · **Status:** stable for V9 implementation · **Code:** `services/reviewer/app/schemas/`

This document defines what goes **into** and comes **out of** the Reviewer
Service. The same contract is served by the deterministic mock and the configurable
OpenAI-compatible LLM reviewer. Specialist selection is separate from scheduling: the reviewer supports sequential specialist calls, one shared single-pass request, or parallel specialist calls.

It extends [`review-job-contract.md`](review-job-contract.md) (webhook →
orchestrator → database). The Celery worker side — how a worker runs one review
and reports job status — is specified in
[`review-worker-interface.md`](review-worker-interface.md).

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

* `schema_version` is `"1.3"` and is present on every request and result.
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
| `schema_version` | `"1.3"` | no (default `"1.3"`) | any other value is rejected |
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
| `schema_version` | `"1.3"` | |
| `job_id`, `repository`, `pr_number`, `commit_sha` | | Echo of the request |
| `reviewer` | `{name, version, mode}` | Which implementation produced this |
| `agents_run` | `ReviewAgent[]` | Agents that ran, in configured order |
| `agent_runs` | `AgentRun[]` | Per-agent duration, finding count, token usage and model metadata |
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
have a file before PR-level ones, then `file`, `line`, `agent`, `id`. The GitHub publisher uses this order; the Orchestrator stores them in this order.

### `AgentRun`

`agent_runs` contains one row for each specialist invocation that completed. It is
separate from `agents_run`: the latter records which agents ran; `agent_runs`
records their measured execution details.

| Field | Type | Meaning |
| --- | --- | --- |
| `agent` | `ReviewAgent` | Specialist identity |
| `duration_ms` | integer | Wall-clock time for this agent invocation |
| `findings_count` | integer | Number of validated findings returned by that agent before aggregation |
| `rejected_findings_count` | integer | Model candidates rejected by schema or changed-line validation; defaults to `0` |
| `response_status` | `valid`, `empty` or `partial` | Distinguishes valid findings, a valid empty response, and a response where some candidates were rejected |
| `input_tokens`, `output_tokens` | integer or null | Provider-reported usage when available; local endpoints may omit it |
| `model` | string or null | Configured model identifier |
| `source_context_files` | integer | Number of changed source files retrieved at the reviewed commit |
| `input_truncated` | boolean | Whether the diff was shortened to respect `MAX_DIFF_CHARS` |
| `reused_checkpoint` | boolean | Whether this completed result was restored from a matching durable checkpoint |
| `shared_call` | boolean | Whether this agent perspective was returned by one shared LLM request |

`agent_runs` is `[]` for deterministic mock reviews and for an empty diff. The
Orchestrator stores these values for the Control Room and Prometheus metrics.

### `ReviewExecution`

The optional `execution` object records pipeline-level metadata so shared-call usage is counted once:

| Field | Meaning |
| --- | --- |
| `strategy` | `sequential`, `single_pass`, `parallel` or `mock` |
| `llm_requests` | Number of successful specialist requests represented by the completed review; resumed checkpoints retain their original request in this total |
| `input_tokens`, `output_tokens` | Pipeline token totals, counted once per request where provider usage is available |
| `duration_ms` | End-to-end reviewer analysis duration |
| `checkpoints_reused` | Number of prior completed specialist passes restored on retry |
| `selected_agents` | Configured specialist roster completed by the review |

In `single_pass`, `AgentRun.shared_call=true` and per-agent duration/token fields are not independently attributed. Pipeline-level usage is authoritative.

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

## 6. Reviewer modes

### Mock mode

When `REVIEWER_MODE=mock`, the service runs `MockReviewer`: no network, no API keys, fully
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

### LLM mode

Set `REVIEWER_MODE=llm`. The Reviewer calls the configured OpenAI-compatible
`/chat/completions` endpoint. `REVIEW_AGENTS` selects a non-empty comma-separated roster. `REVIEW_EXECUTION_STRATEGY=sequential` runs one specialist request at a time and checkpoints each success; `single_pass` submits one request for all selected perspectives; `parallel` runs independent requests concurrently. The legacy `REVIEWER_PIPELINE_MODE` setting remains accepted for backward compatibility but should not be used for new deployments. The system assigns agent identity; model-authored
agent fields are ignored. Findings must pass schema validation and point to an
actual added line when a line is supplied. Rejected candidates are counted in
`AgentRun`; a response containing candidates where none survive validation is
an explicit review failure, not an empty review. Exact duplicates are
aggregated, and high-signal added-line checks (e.g. committed credentials,
`eval`, `shell=True`) supplement the model.

When `GITHUB_TOKEN` is configured, bounded source excerpts are fetched for
changed files at the exact `commit_sha`. The reviewer does not clone or send
the whole repository. `MAX_SOURCE_CONTEXT_CHARS`, `MAX_CONTEXT_FILES` and
`SOURCE_CONTEXT_LINES` bound this context. If `MAX_DIFF_CHARS` is exceeded,
the reviewer records `input_truncated` and lists omitted file patches in the
model input rather than silently treating the partial diff as complete.

Repository-specific rules are fetched from the Orchestrator before analysis.
Optional Langfuse v4 tracing is disabled unless explicitly enabled and records
metadata/usage only; it does not capture the source diff.

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

## 8. Notes for service owners

* **GitHub publisher:** `findings` are included in one consolidated PR-level issue
  comment. The publisher updates its marker-based comment on subsequent reviews;
  it does not create individual inline review threads. In LLM mode, the reviewer
  fetches the GitHub diff when `diff` is omitted, or accepts a supplied diff for
  offline evaluation and deterministic tests.
* **P3 (queue/worker):** call `POST /review` with the request above, apply the
  retry rule in section 5, and report job state (`QUEUED` → `RUNNING` →
  `COMPLETED`/`FAILED`) as specified in
  [`review-worker-interface.md`](review-worker-interface.md).
  `POST /jobs` is idempotent on `(repository, pr_number, commit_sha)`, so a webhook
  redelivery returns the *same* `job_id`; a result is always tied to that `job_id`,
  and a new commit on the PR (or a retry after `FAILED`) gets a new one.
* **Orchestrator:** persists each completed `ReviewResult` payload and normalized
  `review_findings` rows in the same transaction as the `COMPLETED` state.
  `review_agent_runs` stores one timing/token row per invoked agent. The audit
  stream also records GitHub delivery outcomes. Do not store summary counts as
  separate truth; derive them from the normalized findings.
* **P5 (DevOps/QA):** use the files in `examples/` for contract tests, and the
  scenarios above to test failure and retry paths in the compose stack.

## 9. Changelog

| Version | Change |
| --- | --- |
| 1.0 | Initial request/result contract (Weeks 1–3) |
| 1.1 | Add per-agent timing, finding counts, token usage and model metadata; document LLM and multi-agent pipeline |
| 1.2 | Add rejected-candidate, response-status, source-context and diff-truncation telemetry; enforce added-line evidence for LLM findings |
| 1.3 | Separate specialist selection from scheduling; add sequential/single-pass/parallel strategies, execution-level usage, durable retry checkpoints, and maintained GitHub PR report comments |
