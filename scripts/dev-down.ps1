[CmdletBinding()]
param(
    [switch]$RemoveVolumes
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw 'Docker CLI was not found.'
}

if ($RemoveVolumes) {
    Write-Warning 'This deletes the persisted PostgreSQL, Redis and Grafana volumes.'
    docker compose down -v
}
else {
    docker compose down
}

if ($LASTEXITCODE -ne 0) {
    throw 'Docker Compose shutdown failed.'
}
