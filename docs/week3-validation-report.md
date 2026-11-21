> **Historical snapshot:** This document records the original Week 3 / V2 implementation state. It predates V3–V9 work and contains statements about features that were not implemented at that time. It is retained for project history; use [the current README](../README.md), [completion matrix](completion-matrix.md), and [current contracts](contracts/) for the actual system behavior.

# Week 3 P3 validation report

## Automated checks executed in the build environment

| Service | Result |
|---|---:|
| Orchestrator | 19 passed |
| Reviewer | 254 passed, 1 skipped |
| Webhook | 27 passed |
| Gateway | 9 passed |
| **Total** | **309 passed, 1 skipped** |

The skipped Reviewer module is `test_week3_celery.py`: Celery was not installed
in this isolated build environment and network access was unavailable to install
it. The dependency is explicitly pinned in `services/reviewer/requirements.txt`
and included by the root `requirements-dev.txt`, so the Windows test setup and
Docker image build install it normally.

Additional static checks passed:

- Python `compileall` for services, scripts and tests.
- Docker Compose YAML parsed successfully and contains the expected seven
  services: Redis, PostgreSQL, Orchestrator, Reviewer, review-worker, Webhook,
  and Gateway.
- The Control Room's inline JavaScript passed `node --check`.

## Not executed in this build environment

- Docker Compose runtime, Redis broker and Celery worker end-to-end execution.
- PowerShell scripts on Windows (`test_week3.ps1`, `smoke_week2.ps1`,
  `smoke_week3.ps1`).
- Retry and permanent-failure scenarios against the running containers.

Those checks are provided in `scripts/` and must be run on the Windows machine
with Docker Desktop running. Do not describe the runtime smoke test as passed
until `scripts/smoke_week3.ps1` prints `0 failed` locally.
