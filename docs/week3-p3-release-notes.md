> **Historical snapshot:** This document records the original Week 3 / V2 implementation state. It predates V3–V9 work and contains statements about features that were not implemented at that time. It is retained for project history; use [the current README](../README.md), [completion matrix](completion-matrix.md), and [current contracts](contracts/) for the actual system behavior.

# Fidelis Week 3 — P3 release notes

## Implemented

- Added Redis 7 as the Celery broker/result backend and persisted Redis data volume.
- Added Celery producer configuration to the Orchestrator.
- Added a transactional `review_job_outbox` record alongside each newly created job.
- Added `fidelis.run_review` Celery task and a dedicated `review-worker` container.
- Reused P1's existing `ReviewWorker`, reviewer HTTP client, error contract and retry/backoff policy.
- Added an authenticated worker-to-Orchestrator status endpoint. The Orchestrator remains the only writer of job status in PostgreSQL.
- Implemented `QUEUED → RUNNING → COMPLETED`, retry transition `RUNNING → QUEUED`, and terminal `FAILED` behavior.
- Updated the Control Room for Week 3 queue/worker behavior.
- Added local service test runner and Docker-backed Week 3 smoke test; retained the Week 2 regression smoke test.

## P3 boundary / coordination

P3's Week 3 deliverable is Redis/Celery dispatch, worker execution orchestration, and lifecycle coordination. The design uses P1's existing worker contract and deterministic mock rather than building a new AI reviewer. The worker reports status over an authenticated HTTP API; the Orchestrator commits it to PostgreSQL. P4 should review and own the database persistence contract for lifecycle state and later review findings. P5 owns the end-to-end failure and Docker integration verification.

## Explicitly not included

- Real LLM/LangGraph review execution (planned for Week 4).
- Persisting individual findings/review result records in PostgreSQL (later P4 work).
- Multi-agent aggregation, repository learning, or posting review comments to GitHub.

## Delivery semantics

The outbox makes job creation and dispatch intent atomic in PostgreSQL. Publishing to Redis is at-least-once: an ambiguous publisher/commit failure can cause a task message to be published more than once. The job state machine prevents terminal jobs from being re-entered; a future hardening step can add an execution lease/claim token to prevent concurrent duplicate deliveries from both entering `RUNNING`.
