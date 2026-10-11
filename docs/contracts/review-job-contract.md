# Review Job Contract (Week 1)

## Webhook → Orchestrator
POST to Orchestrator's `/jobs` with:
{
    "repository": "owner/repo",
    "pr_number": 42,
    "commit_sha": "abc123"
}

## Orchestrator → Webhook (response)
{
    "job_id": "uuid",
    "status": "QUEUED"
}

## Orchestrator → P4 (DB requirement, Week 1)
P4 owns the eventual Postgres implementation of ReviewJob. Fields:
id, repository, pr_number, commit_sha, status, created_at, started_at, completed_at, error_message

Statuses (fixed set, do not extend without team agreement):
QUEUED, RUNNING, COMPLETED, FAILED

## Orchestrator → P5 (DevOps requirement)
GET /health → 200 {"status": "healthy"}

## Week 2 additions (backward compatible)

### Idempotency
POST /jobs is idempotent on (repository, pr_number, commit_sha).
- New job: 201, "duplicate": false
- Same key, existing non-FAILED job: 200, same job_id, "duplicate": true
- Same key, previous job FAILED: new job, 201
A new commit on the same PR always creates a new job.

### Webhook -> Orchestrator mapping
repo_full_name -> repository, pr_number -> pr_number, head_sha -> commit_sha.
Only pull_request actions in REVIEW_TRIGGER_ACTIONS (webhook/schemas.py) are forwarded.

### Failure behaviour
Orchestrator down/5xx: webhook retries (2x, short backoff), then answers GitHub 502
and forgets the delivery ID so a GitHub redelivery is processed.
Orchestrator 4xx: webhook answers 500 (payload bug, not an outage).

### New endpoint
GET /jobs?repository=&pr_number=&status=&limit=  -> list, newest first (debugging)

### Validation (422)
repository must match owner/repo; pr_number > 0; commit_sha non-empty.

### For P4
Suggested guard against duplicate jobs from concurrent requests across processes:
a unique partial index on review_jobs (repository, pr_number, commit_sha)
WHERE status != 'FAILED'. The orchestrator already handles the resulting
IntegrityError. Not needed for single-process dev; I did not touch models.py.

## Orchestrator / worker → Reviewer
See [`reviewer-contract.md`](reviewer-contract.md) for the request and result of
`POST /review`, and [`review-worker-interface.md`](review-worker-interface.md) for
how a worker executes a job and reports `QUEUED`/`RUNNING`/`COMPLETED`/`FAILED`.
