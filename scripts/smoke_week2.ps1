# Week 2 end-to-end smoke test.
# Run from the repo root with the stack up (docker compose up --build -d):
#   powershell -ExecutionPolicy Bypass -File scripts/smoke_week2.ps1

param(
  [string]$Orch     = "http://localhost:8001",
  [string]$Webhook  = "http://localhost:8002",
  [string]$Reviewer = "http://localhost:8003",
  [string]$Gateway  = "http://localhost:8000"
)

$ErrorActionPreference = "Continue"
$Secret = if ($env:GITHUB_WEBHOOK_SECRET) { $env:GITHUB_WEBHOOK_SECRET } else { "dev_webhook_secret_change_me" }
$PgUser = if ($env:POSTGRES_USER) { $env:POSTGRES_USER } else { "fidelis_user" }
$PgDb   = if ($env:POSTGRES_DB) { $env:POSTGRES_DB } else { "pr_reviewer" }

$SamplePath = "services\webhook\tests\sample_payloads\pull_request_opened.json"
if (-not (Test-Path $SamplePath)) { throw "Run this script from the repository root; missing $SamplePath" }
$Sample = Resolve-Path $SamplePath
$SampleText = [IO.File]::ReadAllText($Sample)
$SampleObj = $SampleText | ConvertFrom-Json
$Repo = $SampleObj.repository.full_name
$PrNumber = $SampleObj.pull_request.number
$OldSha = $SampleObj.pull_request.head.sha

$Run = [guid]::NewGuid().ToString("N").Substring(0, 8)
$HasDocker = [bool](Get-Command docker -ErrorAction SilentlyContinue)
$script:Pass = 0
$script:Fail = 0

function Assert($Cond, [string]$Msg) { if (-not $Cond) { throw $Msg } }

function Check([string]$Name, [scriptblock]$Test) {
  try {
    & $Test | Out-Null
    Write-Host "[PASS] $Name" -ForegroundColor Green
    $script:Pass++
  } catch {
    Write-Host "[FAIL] $Name -> $($_.Exception.Message)" -ForegroundColor Red
    $script:Fail++
  }
}

function New-Sha { -join ((1..40) | ForEach-Object { '{0:x}' -f (Get-Random -Maximum 16) }) }

function New-Body([string]$Action, [string]$Sha) {
  $text = $SampleText.Replace($OldSha, $Sha)
  $text = ([regex]'"action"\s*:\s*"[^"]*"').Replace($text, ('"action": "' + $Action + '"'), 1)
  return [Text.Encoding]::UTF8.GetBytes($text)
}

function Sign([byte[]]$Bytes) {
  $h = New-Object System.Security.Cryptography.HMACSHA256
  try {
    $h.Key = [Text.Encoding]::UTF8.GetBytes($Secret)
    return "sha256=" + ([BitConverter]::ToString($h.ComputeHash($Bytes)) -replace '-', '').ToLower()
  } finally { $h.Dispose() }
}

function Send-Webhook([string]$Delivery, [byte[]]$Body, [string]$Event = "pull_request", [string]$Sig = "") {
  if (-not $Sig) { $Sig = Sign $Body }
  $headers = @{
    "X-GitHub-Event" = $Event
    "X-GitHub-Delivery" = $Delivery
    "X-Hub-Signature-256" = $Sig
  }
  try {
    $r = Invoke-WebRequest -Method Post -Uri "$Webhook/webhook/github" -Headers $headers `
      -ContentType "application/json" -Body $Body -UseBasicParsing -ErrorAction Stop
    return @{ Code = [int]$r.StatusCode; Json = ($r.Content | ConvertFrom-Json) }
  } catch {
    if ($null -eq $_.Exception.Response) { throw }
    return @{ Code = [int]$_.Exception.Response.StatusCode; Json = $null }
  }
}

function Get-Code([string]$Url) {
  try { return [int](Invoke-WebRequest $Url -UseBasicParsing -ErrorAction Stop).StatusCode }
  catch { if ($null -eq $_.Exception.Response) { throw }; return [int]$_.Exception.Response.StatusCode }
}

function Post-Code([string]$Url, $Obj) {
  try {
    return [int](Invoke-WebRequest -Method Post -Uri $Url -ContentType "application/json" `
      -Body ($Obj | ConvertTo-Json -Depth 5) -UseBasicParsing -ErrorAction Stop).StatusCode
  } catch { if ($null -eq $_.Exception.Response) { throw }; return [int]$_.Exception.Response.StatusCode }
}

function Count-JobsFor([string]$Sha) {
  # Depending on the PowerShell/.NET response shape, the jobs endpoint may be
  # exposed either as a bare array or as an object with a `value` collection.
  # Unwrap the latter before matching job fields.
  $response = Invoke-RestMethod "$Orch/jobs?limit=200" -ErrorAction Stop
  $valueProperty = $response.PSObject.Properties['value']

  if ($null -ne $valueProperty) {
    $all = @($valueProperty.Value)
  } else {
    $all = @($response)
  }

  $matches = @($all | Where-Object {
    $null -ne $_ -and [string]$_.commit_sha -eq $Sha
  })
  return [int]$matches.Count
}

function Wait-Healthy([string]$Url, [int]$Seconds = 60) {
  $end = (Get-Date).AddSeconds($Seconds)
  while ((Get-Date) -lt $end) {
    try { Invoke-RestMethod $Url -TimeoutSec 2 -ErrorAction Stop | Out-Null; return $true }
    catch { Start-Sleep -Seconds 1 }
  }
  return $false
}

Write-Host "`n== 1. Service health ==" -ForegroundColor Cyan
Check "orchestrator /health" { Assert ((Invoke-RestMethod "$Orch/health" -ErrorAction Stop).status -eq "healthy") "unexpected body" }
Check "webhook /healthz" { Assert ((Invoke-RestMethod "$Webhook/healthz" -ErrorAction Stop).status -eq "ok") "unexpected body" }
Check "reviewer /healthz" { Invoke-RestMethod "$Reviewer/healthz" -ErrorAction Stop }
Check "gateway /healthz" { Invoke-RestMethod "$Gateway/healthz" -ErrorAction Stop }

Write-Host "`n== 2. Webhook security and filtering ==" -ForegroundColor Cyan
$sha1 = New-Sha
$body1 = New-Body "opened" $sha1
Check "bad signature -> 401" {
  $r = Send-Webhook "$Run-bad" $body1 "pull_request" "sha256=deadbeef"
  Assert ($r.Code -eq 401) "got HTTP $($r.Code)"
}
Check "non-PR event (push) is ignored" {
  $r = Send-Webhook "$Run-push" $body1 "push"
  Assert ($r.Code -eq 200 -and $r.Json.status -eq "ignored") "got HTTP $($r.Code), status '$($r.Json.status)'"
}
Check "non-trigger action (closed) is ignored and creates no job" {
  $closedSha = New-Sha
  $r = Send-Webhook "$Run-closed" (New-Body "closed" $closedSha)
  Assert ($r.Json.status -eq "ignored") "got '$($r.Json.status)'"
  $count = Count-JobsFor $closedSha
  Assert ($count -eq 0) "expected 0 jobs for a closed PR, found $count"
}

Write-Host "`n== 3. Happy path: PR event -> job ==" -ForegroundColor Cyan
Check "valid PR event -> queued with job_id" {
  $r = Send-Webhook "$Run-d1" $body1
  Assert ($r.Code -eq 200) "HTTP $($r.Code)"
  Assert ($r.Json.status -eq "queued") "status '$($r.Json.status)'"
  Assert ($r.Json.job_id) "no job_id returned"
  $script:JobId = $r.Json.job_id
}
Check "job stored in orchestrator with correct fields" {
  $j = Invoke-RestMethod "$Orch/jobs/$script:JobId" -ErrorAction Stop
  Assert (@("QUEUED", "RUNNING", "COMPLETED") -contains $j.status) "unexpected status $($j.status)"
  Assert ($j.repository -eq $Repo) "repository $($j.repository)"
  Assert ($j.pr_number -eq $PrNumber) "pr_number $($j.pr_number)"
  Assert ($j.commit_sha -eq $sha1) "commit_sha $($j.commit_sha)"
  if ($j.status -eq "QUEUED") { Assert ($null -eq $j.started_at) "queued job should not have started_at" }
  if ($j.status -in @("RUNNING", "COMPLETED")) { Assert ($j.started_at) "started_at should be set after worker start" }
}

Write-Host "`n== 4. Idempotency ==" -ForegroundColor Cyan
Check "same delivery ID replayed -> duplicate_ignored" {
  $r = Send-Webhook "$Run-d1" $body1
  Assert ($r.Json.status -eq "duplicate_ignored") "got '$($r.Json.status)'"
}
Check "new delivery, same commit -> same job_id, duplicate=true" {
  $r = Send-Webhook "$Run-d2" $body1
  Assert ($r.Json.job_id -eq $script:JobId) "got a different job_id"
  Assert ($r.Json.duplicate -eq $true) "duplicate flag not set"
  $count = Count-JobsFor $sha1
  Assert ($count -eq 1) "expected exactly 1 job for this commit, found $count"
}
Check "new commit on same PR -> new job" {
  $sha2 = New-Sha
  $r = Send-Webhook "$Run-d3" (New-Body "synchronize" $sha2)
  Assert ($r.Json.status -eq "queued") "got '$($r.Json.status)'"
  Assert ($r.Json.job_id -ne $script:JobId) "reused the old job_id"
  $count = Count-JobsFor $sha2
  Assert ($count -eq 1) "expected 1 job for new commit, found $count"
}

Write-Host "`n== 5. Orchestrator API edge cases ==" -ForegroundColor Cyan
Check "unknown job id -> 404" { Assert ((Get-Code "$Orch/jobs/does-not-exist") -eq 404) "not 404" }
Check "invalid repository -> 422" {
  Assert ((Post-Code "$Orch/jobs" @{ repository = "norepo"; pr_number = 1; commit_sha = "abc" }) -eq 422) "not 422"
}
Check "invalid pr_number -> 422" {
  Assert ((Post-Code "$Orch/jobs" @{ repository = "a/b"; pr_number = 0; commit_sha = "abc" }) -eq 422) "not 422"
}

Write-Host "`n== 6. Persistence ==" -ForegroundColor Cyan
if ($HasDocker) {
  Check "job row exists in Postgres" {
    $n = (docker compose exec -T postgres psql -U $PgUser -d $PgDb -t -A -c "select count(*) from review_jobs where id='$script:JobId'" 2>&1 | Out-String).Trim()
    Assert ($n -eq "1") "psql returned '$n'"
  }
  Check "job survives an orchestrator restart" {
    docker compose restart orchestrator 2>&1 | Out-Null
    Assert (Wait-Healthy "$Orch/health" 90) "orchestrator did not come back"
    $j = Invoke-RestMethod "$Orch/jobs/$script:JobId" -ErrorAction Stop
    Assert ($j.job_id -eq $script:JobId) "job missing after restart"
  }
} else { Write-Host "[SKIP] Docker not found, skipping persistence checks" -ForegroundColor Yellow }

Write-Host "`n== 7. Orchestrator outage ==" -ForegroundColor Cyan
if ($HasDocker) {
  $sha3 = New-Sha
  $body3 = New-Body "opened" $sha3
  try {
    docker compose stop orchestrator 2>&1 | Out-Null
    Check "orchestrator down -> webhook answers 502 (takes ~8s)" {
      $r = Send-Webhook "$Run-d4" $body3
      Assert ($r.Code -eq 502) "got HTTP $($r.Code)"
    }
  } finally {
    docker compose start orchestrator 2>&1 | Out-Null
    [void](Wait-Healthy "$Orch/health" 90)
  }
  Check "GitHub redelivery (same delivery ID) succeeds after recovery" {
    $r = Send-Webhook "$Run-d4" $body3
    Assert ($r.Json.status -eq "queued") "got '$($r.Json.status)'"
    $count = Count-JobsFor $sha3
    Assert ($count -eq 1) "expected 1 job after recovery, found $count"
  }
} else { Write-Host "[SKIP] Docker not found, skipping outage test" -ForegroundColor Yellow }

Write-Host ""
$color = if ($script:Fail -eq 0) { "Green" } else { "Red" }
Write-Host "$($script:Pass) passed, $($script:Fail) failed" -ForegroundColor $color
if ($script:Fail -gt 0) { exit 1 }
