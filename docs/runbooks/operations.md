# Fidelis operations runbook

## Startup

```bash
cp .env.example .env  # first time only
# replace all development secrets
docker compose up -d --build --wait
docker compose ps
docker compose logs -f orchestrator review-worker reviewer
```

Expected HTTP health endpoints: Gateway `:8000/healthz`, Orchestrator `:8001/health`, Webhook `:8002/healthz`, Reviewer `:8003/healthz`, Learner `:8004/healthz`. Postgres/Redis have their own container healthchecks. Review and learning workers are separate Celery processes.

## Local model readiness

The default compose profile points to `http://host.docker.internal:8080/v1`. Start the host's llama.cpp server before submitting a real review, bind it to a host-reachable interface (for example `0.0.0.0:8080`), and ensure `LLM_MODEL` matches the server's loaded model alias. The four default specialists run sequentially. If using a cloud endpoint, set its URL/key/model and choose `REVIEW_EXECUTION_STRATEGY=single_pass`.

## PR review flow

1. Configure a GitHub webhook for `pull_request` at `/webhook/github` using `GITHUB_WEBHOOK_SECRET`.
2. Make sure the event is one of `opened`, `synchronize`, or `reopened`.
3. Inspect the job in the Control Room or `GET /api/orchestrator/jobs`.
4. Open the job details to inspect events, findings and agent execution metrics.
5. To replay, use the row action or `POST /jobs/{job_id}/replay`. A replay creates a new review job; it does not mutate the old result. A transient retry of the same job can restore completed agent checkpoints when the review context fingerprint still matches.

For a local path with no GitHub account, use **Send sample PR event** in the Control Room. It is a synthetic, correctly signed webhook event.

## Broker outage / stuck QUEUED jobs

The Orchestrator writes an outbox record with every job. Immediate publish failures leave the record pending; the periodic sweeper retries every five seconds if `ASYNC_REVIEW_ENABLED=true`. Inspect orchestrator logs for `outbox sweep could not publish`. Check Redis health with `docker compose exec redis redis-cli ping`; then inspect `docker compose logs review-worker`. Do not delete the outbox row manually.

## Worker failures

- `FAILED`: inspect `/jobs/{id}` and `/jobs/{id}/events`; `error_message` records the last terminal cause.
- Repeated upstream failures: inspect Reviewer logs and LLM/GitHub credentials.
- Status callback failures: verify `WORKER_STATUS_TOKEN` matches in Orchestrator, Reviewer and Learner; the Celery task retries infrastructure-reporting separately.
- A job stuck `RUNNING` after process termination may need operator replay if Celery redelivery does not recover it. The project does not yet implement a time-based lease reaper.

## GitHub delivery failures

A review can be `COMPLETED` even if its GitHub comment fails. Inspect the audit event `github_review_failed` or `github_review_skipped`. Check `GITHUB_TOKEN` has Issues: Read and write permissions and inspect GitHub API rate limits. The persisted result remains available.

## Learner failures

Start learning with a repository that the token can read. Inspect the learner task response and `docker compose logs learning-worker`. If no matching path/patch evidence exists, the learner can successfully store zero rules; that is not a failed review system. Rules are advisory.

## Monitoring

- Prometheus: `http://localhost:9090/targets`; Orchestrator target should be `UP`.
- Grafana: `http://localhost:3000`; provisioned dashboard is `Fidelis Overview`.
- Langfuse: only receives traces when all enable/key variables are configured. Missing SDK or trace-service errors should be warnings and must not fail reviews.

## Reset

```bash
docker compose down          # retain Postgres/Redis/Grafana named volumes
docker compose down -v       # destructive: deletes persistent volumes
```

Use the destructive reset only for disposable local data.
