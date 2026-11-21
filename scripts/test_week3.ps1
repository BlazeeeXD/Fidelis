# Run the four service unit/API suites locally (no Docker/Redis required).
# Install first: python -m pip install -r requirements-dev.txt
# Run from any directory:
#   powershell -ExecutionPolicy Bypass -File scripts/test_week3.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Push-Location $Root
try {
  # Orchestrator tests use an isolated SQLite database. Runtime Compose uses PostgreSQL.
  $env:DATABASE_URL = "sqlite://"
  $env:ASYNC_REVIEW_ENABLED = "false"
  $env:WORKER_STATUS_TOKEN = "week3-local-test-token"

  $suites = @(
    "services\orchestrator",
    "services\reviewer",
    "services\webhook",
    "services\gateway"
  )
  foreach ($suite in $suites) {
    Write-Host "`n== $suite ==" -ForegroundColor Cyan
    Push-Location $suite
    try {
      python -m pytest tests -q
      if ($LASTEXITCODE -ne 0) { throw "Tests failed in $suite (exit $LASTEXITCODE)" }
    } finally { Pop-Location }
  }
  Write-Host "`nAll local service test suites passed." -ForegroundColor Green
  Write-Host "Next: run scripts/smoke_week3.ps1 with Docker Compose up to verify Redis + Celery end-to-end." -ForegroundColor Yellow
} finally {
  Pop-Location
}
