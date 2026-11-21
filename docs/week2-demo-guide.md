# Fidelis Week 2 Demo Guide

## The one-sentence explanation

Fidelis receives a GitHub pull-request webhook, verifies that it is signed with the shared secret, ignores irrelevant or repeated deliveries, and creates a review-job record in PostgreSQL that can be retrieved later.

## Start the system

From the repository root, with Docker Desktop running:

```powershell
docker compose up --build -d
docker compose ps
```

Wait until PostgreSQL and the services are healthy, then open **http://localhost:8000**. This is the Week 2 Control Room served by the Gateway.

## A 3-minute walkthrough

1. **Show service health.** Explain that the page probes the Gateway, Webhook, Orchestrator, and Reviewer over their actual HTTP health endpoints. Reviewer health is shown because it is part of the stack, but Reviewer is not called by the Week 2 job-creation path.
2. **Click “Send sample PR event.”** The Gateway demo endpoint builds a synthetic pull-request payload, generates a fresh commit SHA, calculates an HMAC-SHA256 signature with the configured local webhook secret, and sends the signed request to the real Webhook HTTP handler. The Webhook validates and parses the event, then calls the Orchestrator. The Orchestrator returns a job ID and persists the job in PostgreSQL. The new `QUEUED` job should appear in the live jobs table.
3. **Click “Replay delivery.”** It resends the same payload bytes, signature, and delivery ID. The Webhook should report `duplicate_ignored`; the job list should not gain a second job for that delivery.
4. **Click “Simulate new commit.”** This sends the same sample repository and PR number with a new commit SHA. The expected behavior is a different job ID, because a new commit deserves a new review job.
5. **Show the test map.** The lower section explains the 19 assertions in `scripts/smoke_week2.ps1`, including negative security tests, API validation, direct PostgreSQL verification, restart persistence, and upstream outage/recovery.
6. **Run the smoke test for formal evidence.** In a separate Command Prompt or PowerShell window, run:

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts/smoke_week2.ps1
   ```

   The expected result for the current Week 2 implementation is `19 passed, 0 failed`.

## What each part proves

| Area | What is exercised | Why it matters |
| --- | --- | --- |
| Signature verification | An invalid signature returns 401 | Attackers must not be able to forge accepted GitHub events |
| Event filtering | Pushes and closed PRs are ignored | Unrelated events must not create jobs |
| Happy path | A valid PR event creates a job with the correct repo, PR, and SHA | Connects the independently written services |
| Idempotency | Delivery replay is ignored; duplicate commit returns existing job; a new SHA creates a new job | Webhook retries must not multiply work, but a new commit must not be skipped |
| API validation | Unknown job returns 404; invalid fields return 422 | Bad requests fail predictably |
| Persistence | Job exists in PostgreSQL and remains retrievable after Orchestrator restart | Accepted work must survive service restarts |
| Outage recovery | Webhook returns 502 while Orchestrator is down; redelivery works after recovery | Failures remain visible and retryable |

## Scope honesty

Week 2 is the **reliable intake and job-persistence milestone**. A job remains `QUEUED` by design. Redis, Celery workers, AI review execution, findings, GitHub comments, and repository learning are later milestones. Do not describe this demo as an AI code reviewer yet.

The browser's sample event is synthetic, not a live GitHub.com delivery. It uses the same Webhook handler and HMAC validation, which is what makes it useful for a repeatable classroom demo. The PowerShell smoke test separately covers forged signatures, Postgres row existence, real container restart, and outage/recovery.

The visual event injection and development token endpoints are disabled when `ENV=production`. Do not use the local demo defaults as production configuration.
