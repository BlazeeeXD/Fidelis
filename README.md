# Fidelis

AI-powered GitHub PR reviewer: a webhook service receives GitHub events, an
orchestrator queues review jobs, and a reviewer service produces the review —
all reached through a single authenticated gateway.

**Week 1 / V0 status:** `docker compose up` starts every service, all health
endpoints respond, and a test event can travel end-to-end
(see `scripts/smoke_test.py`).

## Prerequisites

- Docker Desktop (Engine + Compose v2)
- Python 3.12 (for running tests and the smoke test locally)

## Quickstart

```bash
cp .env.example .env        # dev placeholders; never commit .env
docker compose up --build
```

Wait until `docker compose ps` shows every service as `healthy`, then:

```bash
python scripts/smoke_test.py
```

## Services

Every container listens on port **8000 internally**; host ports differ:

| Service      | Host port | Health URL                                | Notes                                  |
| ------------ | --------- | ----------------------------------------- | -------------------------------------- |
| gateway      | 8000      | http://localhost:8000/healthz             | JWT auth, entry point for all traffic  |
| orchestrator | 8001      | http://localhost:8001/health              | Review job lifecycle (`/health` is fixed by the contract) |
| webhook      | 8002      | http://localhost:8002/healthz             | GitHub webhook receiver (HMAC-verified) |
| reviewer     | 8003      | http://localhost:8003/healthz             | Placeholder app, replaced by the real reviewer in Week 2+ |
| postgres     | 5432      | —                                         | `pg_isready` healthcheck               |

`services/learner` is intentionally **not** in compose — it arrives in Week 8.

Configuration comes from `.env` (copy `.env.example`); compose falls back to
safe dev defaults via `${VAR:-default}` interpolation.

## Running tests

Install dependencies once per service (service requirements don't include
pytest/httpx, hence the root dev file):

```bash
pip install -r requirements-dev.txt
pip install -r services/orchestrator/requirements.txt   # etc.
```

Then run pytest **from inside each service directory**:

```bash
cd services/orchestrator && pytest
cd services/webhook      && pytest   # test file sets GITHUB_WEBHOOK_SECRET itself
cd services/gateway      && pytest
```

Lint (advisory for now): `ruff check services/`

## Smoke test

With the stack running:

```bash
docker compose up -d --build --wait
python scripts/smoke_test.py
```

Standard library only. It checks all four health endpoints, mints a JWT via
`POST /auth/dev-token`, creates and fetches a review job through the gateway,
and delivers a signed GitHub `pull_request` payload — printing PASS/FAIL per
step and exiting non-zero on any failure.

CI (`.github/workflows/ci.yml`) runs the per-service tests, ruff, and this
smoke test against a freshly built compose stack on every PR.

## Troubleshooting

- **Port already in use** — something else holds 8000-8003/5432. Stop it, or
  change the left side of the `ports:` mapping in `docker-compose.yml`.
- **Postgres not healthy / services stuck waiting** — check
  `docker compose logs postgres`; then reset (below) and start again.
- **Reset everything** (drops the postgres volume and all containers):

  ```bash
  docker compose down -v
  ```

- **Any service failing to start**: `docker compose logs <service>`.
