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