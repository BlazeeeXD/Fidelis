# Roadmap completion matrix

This matrix maps the supplied **Revised 10-Week Build Plan** to the implementation in this repository. It records what exists in code and what still requires environment credentials or deployment verification.

| Milestone | Roadmap target | Implementation evidence | State |
|---|---|---|---|
| V0 | Common executable service skeleton | Docker Compose, five FastAPI services, health checks, shared configuration, CI | Implemented |
| V1 | GitHub PR event enters system and creates job | HMAC SHA-256 webhook, action filtering, delivery dedupe, PR extraction, PostgreSQL job persistence, idempotency | Implemented; live GitHub delivery not exercised locally |
| V2 | Queue and worker | Redis, Celery review worker, job state machine, retry policy, transactional outbox, five-second outbox recovery sweep | Implemented; live broker path awaits Docker smoke |
| V3 | One AI reviewer | OpenAI-compatible chat completions, general reviewer prompt, validated structured findings, diff retrieval | Implemented; requires reachable configured LLM for real inference |
| V4 | Multi-agent review | Four configurable specialists with sequential, single-pass and parallel schedulers; deterministic aggregation and durable per-agent checkpoints for retries | Implemented; model quality requires a live LLM evaluation |
| V5 | Review output posted to GitHub PR | One marker-based consolidated issue comment is created or updated; delivery outcomes are audited separately from review completion | Implemented; requires GitHub Issues: Read/Write permission |
| V6 | Review history and dashboard data | Canonical result JSON, normalized findings, agent run rows, append-only lifecycle/delivery audit, replay endpoint, summary metrics, Control Room | Implemented |
| V7 | Repository-specific learning | Celery learner fetches merged PRs and patches, extracts explainable patterns, persists rules, injects them into review context | Implemented; requires GitHub read token for real mining |
| V8 | Reliability and observability | Status reporting retries, outbox sweeper, worker retries, Prometheus exposition, provisioned Grafana, optional Langfuse v4 metadata tracing | Implemented; live dashboards/tracing need Docker and credentials |
| V9 | Feature freeze, stress/evaluation, documentation | Service tests, integration test definitions, labelled benchmark fixtures, sequential-vs-single-pass-vs-parallel evaluation script, async HTTP load test, architecture/runbooks/contracts/security/evaluation docs | Implemented; real LLM quality benchmark and load profile not run without provider/stack |

## Local validation performed

The final local test matrix on this working copy reported:

| Suite | Result |
|---|---:|
| Reviewer | 283 passed, 1 skipped |
| Orchestrator | 35 passed |
| Gateway | 11 passed |
| Webhook | 27 passed |
| Learner | 2 passed |
| **Total** | **358 passed, 1 skipped** |

The one skipped Reviewer test is an optional Celery adapter suite because Celery is not installed in this runtime. Docker-backed integration tests, live model inference and live GitHub delivery were not executable in this environment. Python compilation, generated contract freshness, dashboard JavaScript syntax and Compose YAML/configuration are checked separately; Docker runtime validation still needs a Docker-capable host.

## Remaining environment-dependent actions

1. Run `docker compose up -d --build --wait`, then `python scripts/smoke_test.py` and `python -m pytest tests/integration -v` on a Docker-capable machine.
2. Set a test LLM endpoint and run `python scripts/evaluate_pipelines.py --repeat 2` to collect actual sequential-vs-single-pass quality, latency and token results.
3. Configure a test GitHub App/fine-grained token and webhook to verify real PR diff retrieval, review publication and merged-PR learning.
4. Optionally configure Langfuse and confirm generation spans in the chosen workspace.

## Deliberate limits

The benchmark is a small human-labelled fixture set, not statistically validated ground truth. The learner mines a narrow set of code/path conventions and is not online model training. The deterministic scanner is not a replacement for a full static-analysis or security suite. Before production use, add Alembic schema migrations, a stale-running-job lease reaper, secret-manager integration, deployment network policy, rate limiting and tenant-level authorization. These are not hidden behind a claim of production certification.
