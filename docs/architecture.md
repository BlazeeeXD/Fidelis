# Fidelis architecture

## System boundaries

**Gateway** is the only intended public application entry point. It mints development JWTs only outside production, serves the Control Room and proxies authenticated API traffic. The signed GitHub webhook route is unauthenticated at the gateway because GitHub cannot mint the internal JWT; the Webhook service verifies `X-Hub-Signature-256` with the configured secret.

**Webhook** validates the event type/action and required PR fields, deduplicates the GitHub delivery, and asks the Orchestrator to create a job. Delivery idempotency is distinct from job idempotency: a repeated delivery is ignored; a different delivery for the same repository/PR/SHA maps to the existing active job.

**Orchestrator** owns the database and lifecycle state machine. It commits the job and `review_job_outbox` row atomically. It is the sole writer for job status, result payload, queryable finding rows, per-agent telemetry, audit events and repository rules. Internal worker callbacks require `X-Worker-Token`.

**Review worker** runs as a separate Celery process. It calls the Reviewer API, reports started/retry/completed/failed events, and publishes one maintained, consolidated PR-level issue comment to GitHub by default (configurable off). Review analysis success and GitHub comment delivery are separate outcomes: failure to comment must not destroy a valid review.

**Reviewer** offers deterministic mock mode and an OpenAI-compatible LLM mode behind one contract. LLM mode fetches a PR diff if one was not supplied, reads repository conventions, and retrieves bounded source excerpts at the exact PR head commit for changed files. The selected specialist roster is configured independently from its scheduler: sequential, one shared single-pass generation, or parallel execution. Model findings are schema-validated and line-anchored against added diff lines; malformed or incorrectly anchored candidates are counted, and an all-invalid response fails visibly. The aggregator de-duplicates observations and sorts findings. The high-signal deterministic scanner complements, but does not replace, SAST.

**Learner** is a separate HTTP/Celery service. It fetches recently merged PR metadata and changed-file patches, extracts simple evidence-backed conventions, and stores them using the Orchestrator's internal endpoint. Future LLM reviews receive the rules as advisory context.

## Data model

- `review_jobs`: one row per review attempt; the partial unique index prevents duplicate active `(repository, pr_number, commit_sha)` jobs but permits completed attempts to be replayed.
- `review_job_outbox`: dispatch intent committed with a new job. A publisher marks it after successful broker publication; the sweeper retries unmarked rows. Celery delivery is at-least-once, not exactly-once.
- `review_results`: complete canonical JSON result and reviewer/timing metadata.
- `review_findings`: normalized rows indexed by job, agent, severity and category for dashboard queries.
- `review_agent_runs`: per-agent duration, count, token usage and model metadata.
- `review_agent_checkpoints`: successful per-agent outputs keyed by job and agent, with a context fingerprint; retries reuse only matching checkpoints.
- `review_audit_events`: lifecycle transitions, replay lineage and GitHub delivery outcomes.
- `repository_rules`: unique `(repository, rule_key)` convention records with confidence, evidence counts and source PRs.

## Lifecycle

```text
QUEUED ──► RUNNING ──► COMPLETED
   ▲           │
   └── retry ──┤
               └─────► FAILED
```

`COMPLETED` and `FAILED` are terminal for one attempt. Replay creates a new job, preserving the original record and audit trail. The Celery task uses late acknowledgement/worker-lost redelivery and bounded review retries; infrastructure status-report retries have a separate counter.

## Agent scheduling, checkpoints and aggregation

Agent selection and scheduling are separate settings. The default local profile selects `STATIC_ANALYSIS`, `SECURITY`, `ARCHITECTURE` and `CODE_STYLE` and runs them **sequentially** against one model server. This keeps one local model resident and avoids concurrent inference requests competing for limited VRAM. Each specialist receives the same source/context packet and its own role prompt; it does not inherit another specialist's conclusions, preserving independent analysis.

`REVIEW_EXECUTION_STRATEGY` supports three values:

- `sequential`: one LLM call per specialist, in configured order. After each successful pass, the reviewer persists its findings and telemetry to the Orchestrator checkpoint API. A retry restores only a checkpoint whose SHA-256 fingerprint matches the model, prompt, agent roster, strategy, repository, commit and complete input packet. It then resumes at the first unfinished specialist.
- `single_pass`: one LLM request contains all selected perspectives and returns one findings array per agent. Token usage and model request count are recorded once at pipeline level; per-agent shared-call timings are not misrepresented as independent timings. This is the cost-conscious mode for cloud APIs.
- `parallel`: independent specialist requests execute concurrently. Use only when the inference endpoint has adequate concurrency and capacity.

All strategies pass through the same schema validation, changed-line anchoring, aggregation and deterministic high-signal checks. The aggregator deduplicates equivalent observations and retains the strongest severity/confidence. A missing specialist result in single-pass mode makes the review fail visibly; partial completion is never represented as a complete review.

## GitHub report delivery

The worker writes one marker-based issue comment to the PR containing the complete consolidated report and execution summary. On later reviews, it updates the existing Fidelis comment. The database remains the source of truth; GitHub is the developer-facing delivery surface and the Control Room provides operational visibility. GitHub comment failure does not erase a successful persisted review and is recorded as a separate audit event.

## Security boundaries

- GitHub webhook authenticity: HMAC SHA-256 constant-time comparison.
- Public API: JWT-protected gateway proxies.
- Worker-to-Orchestrator and Gateway-to-Learner calls: shared internal token, never returned to the browser.
- Review output: untrusted model data validated by Pydantic.
- GitHub PR report publishing: enabled by the sample local configuration; disable with `PUBLISH_GITHUB_REVIEWS=false`. Requires issue-comment write permission.
- LLM/Langfuse data handling: Langfuse is opt-in and receives metadata/token counts, not raw diffs.

See [security notes](security.md) and [operations](runbooks/operations.md).
