# CI

Workflow lives at [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml).
Owner of this folder: Siddharth Mor (Week 14-15 per project timeline); the
workflow itself was added by P5 in Week 1 / V0.

## Triggers

- every `pull_request`
- `push` to `main`

## Jobs

### `tests`

Matrix over `orchestrator`, `webhook`, `gateway` on Python 3.12 with pip
caching (keyed on each service's `requirements.txt` + `requirements-dev.txt`).

For each service:

1. `pip install -r services/<svc>/requirements.txt -r requirements-dev.txt`
   (service requirements don't include pytest/httpx, so CI adds the root
   `requirements-dev.txt`: pytest, httpx, ruff)
2. `pytest` with `working-directory: services/<svc>` — tests are run from
   inside the service directory, same as locally. `GITHUB_WEBHOOK_SECRET` is
   injected as `testsecret` (the webhook test sets it itself, but the env var
   keeps imports safe).

### `lint`

`ruff check services/` via `requirements-dev.txt`. Currently set to
`continue-on-error: true` because existing code hasn't been linted yet —
make it blocking once the backlog is cleaned up.

### `docker-smoke`

End-to-end check that `docker compose up` actually works:

1. `cp .env.example .env`
2. `docker compose up -d --build --wait` (waits for all healthchecks)
3. `python scripts/smoke_test.py` (stdlib only): health endpoints on
   :8000-:8003, `POST /auth/dev-token`, create + fetch a review job through
   the gateway, and a signed GitHub `pull_request` webhook delivery
4. `docker compose logs` if anything failed (`if: failure()`)
5. `docker compose down -v` always (`if: always()`)

## Running the same checks locally

```bash
pip install -r services/<svc>/requirements.txt -r requirements-dev.txt
cd services/<svc> && pytest        # orchestrator | webhook | gateway

ruff check services/               # advisory lint

cp .env.example .env
docker compose up -d --build --wait
python scripts/smoke_test.py
docker compose down -v
```
