# CI

Placeholder for GitHub Actions workflows (e.g. `.github/workflows/*.yml`
referencing back to this, or pipeline configs if using another CI
system). Owner: Siddharth Mor (Week 14-15 per project timeline).

Suggested first workflow: on PR, run `pytest` for each service under
`services/*/tests/` that has changed, plus lint (ruff/flake8).
