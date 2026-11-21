[CmdletBinding()]
param(
    [switch]$SkipSmoke
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw 'Docker CLI was not found. Install Docker Desktop and enable the WSL2/Linux container engine.'
}

docker compose version
if ($LASTEXITCODE -ne 0) {
    throw 'Docker Compose v2 is required.'
}

if (-not (Test-Path '.env')) {
    Copy-Item '.env.example' '.env'
    Write-Warning 'Created .env from .env.example. Change the development secrets before exposing the stack.'
}

Write-Host 'Building and starting Fidelis...' -ForegroundColor Cyan
docker compose up --build -d --wait
if ($LASTEXITCODE -ne 0) {
    throw 'Docker Compose failed to start the project. Inspect: docker compose logs'
}

docker compose ps
if ($LASTEXITCODE -ne 0) {
    throw 'Could not list Docker Compose services.'
}

if (-not $SkipSmoke) {
    if (Get-Command python -ErrorAction SilentlyContinue) {
        Write-Host 'Running HTTP and workflow smoke checks...' -ForegroundColor Cyan
        python scripts/smoke_test.py
        if ($LASTEXITCODE -ne 0) {
            throw 'Smoke test failed. Inspect: docker compose logs'
        }
    }
    else {
        Write-Warning 'Python was not found; skipped scripts/smoke_test.py.'
    }
}

Write-Host ''
Write-Host 'Fidelis is up:' -ForegroundColor Green
Write-Host '  Control Room: http://localhost:8000'
Write-Host '  Grafana:      http://localhost:3000'
Write-Host '  Prometheus:   http://localhost:9090'
