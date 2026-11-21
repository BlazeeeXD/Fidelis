# Week 3 end-to-end smoke test (P3: Redis + Celery + worker lifecycle).
# Run from the repository root after: docker compose up --build -d
#   powershell -ExecutionPolicy Bypass -File scripts/smoke_week3.ps1
# Retry variant: set REVIEWER_MOCK_SCENARIO=flaky in .env, recreate reviewer + worker,
# then add -RequireRetry. Permanent-failure variant: set REVIEWER_MOCK_SCENARIO=fail_permanent,
# recreate reviewer + worker, then add -RequireFailure.

param(
  [string]$Orch = "http://localhost:8001",
  [string]$Webhook = "http://localhost:8002",
  [string]$Reviewer = "http://localhost:8003",
  [string]$Gateway = "http://localhost:8000",
  [switch]$RequireRetry,
  [switch]$RequireFailure
)

$ErrorActionPreference = "Continue"
if ($RequireRetry -and $RequireFailure) {
  throw "Choose one optional scenario at a time: -RequireRetry OR -RequireFailure."
}
function Get-ComposeEnvValue([string]$Service, [string]$Name, [string]$Fallback) {
  # Read the effective value from the running container so a custom .env does
  # not require the same variable to be manually exported in PowerShell.
  try {
    $value = (docker compose exec -T $Service printenv $Name 2>$null | Out-String).Trim()
    if ($value) { return $value }
  } catch {}
  return $Fallback
}
$Secret = if ($env:GITHUB_WEBHOOK_SECRET) { $env:GITHUB_WEBHOOK_SECRET } else { Get-ComposeEnvValue "webhook" "GITHUB_WEBHOOK_SECRET" "dev_webhook_secret_change_me" }
$WorkerToken = if ($env:WORKER_STATUS_TOKEN) { $env:WORKER_STATUS_TOKEN } else { Get-ComposeEnvValue "orchestrator" "WORKER_STATUS_TOKEN" "fidelis-dev-worker-token-change-me" }
$PgUser = if ($env:POSTGRES_USER) { $env:POSTGRES_USER } else { Get-ComposeEnvValue "postgres" "POSTGRES_USER" "fidelis_user" }
$PgDb = if ($env:POSTGRES_DB) { $env:POSTGRES_DB } else { Get-ComposeEnvValue "postgres" "POSTGRES_DB" "pr_reviewer" }
$SamplePath = "services\webhook\tests\sample_payloads\pull_request_opened.json"
if (-not (Test-Path $SamplePath)) { throw "Run from repository root; missing $SamplePath" }
$SampleText = [IO.File]::ReadAllText((Resolve-Path $SamplePath))
$SampleObj = $SampleText | ConvertFrom-Json
$Repo = $SampleObj.repository.full_name
$PrNumber = $SampleObj.pull_request.number
$OldSha = $SampleObj.pull_request.head.sha
$Run = [guid]::NewGuid().ToString("N").Substring(0, 8)
$ExpectedFinalStatus = if ($RequireFailure) { "FAILED" } else { "COMPLETED" }
$script:Pass = 0
$script:Fail = 0

function Assert($Cond, [string]$Msg) { if (-not $Cond) { throw $Msg } }
function Check([string]$Name, [scriptblock]$Test) {
  try { & $Test | Out-Null; Write-Host "[PASS] $Name" -ForegroundColor Green; $script:Pass++ }
  catch { Write-Host "[FAIL] $Name -> $($_.Exception.Message)" -ForegroundColor Red; $script:Fail++ }
}
function New-Sha { -join ((1..40) | ForEach-Object { '{0:x}' -f (Get-Random -Maximum 16) }) }
function New-Body([string]$Action, [string]$Sha) {
  $text = $SampleText.Replace($OldSha, $Sha)
  $text = ([regex]'"action"\s*:\s*"[^"]*"').Replace($text, ('"action": "' + $Action + '"'), 1)
  return [Text.Encoding]::UTF8.GetBytes($text)
}
function Sign([byte[]]$Bytes) {
  $h = New-Object System.Security.Cryptography.HMACSHA256
  try { $h.Key = [Text.Encoding]::UTF8.GetBytes($Secret); return "sha256=" + ([BitConverter]::ToString($h.ComputeHash($Bytes)) -replace '-', '').ToLower() }
  finally { $h.Dispose() }
}
function Send-Webhook([string]$Delivery, [byte[]]$Body, [string]$Event = "pull_request", [string]$Sig = "") {
  if (-not $Sig) { $Sig = Sign $Body }
  $headers = @{ "X-GitHub-Event"=$Event; "X-GitHub-Delivery"=$Delivery; "X-Hub-Signature-256"=$Sig }
  try {
    $r = Invoke-WebRequest -Method Post -Uri "$Webhook/webhook/github" -Headers $headers -ContentType "application/json" -Body $Body -UseBasicParsing -ErrorAction Stop
    return @{ Code=[int]$r.StatusCode; Json=($r.Content | ConvertFrom-Json) }
  } catch {
    if ($null -eq $_.Exception.Response) { throw }
    $code = [int]$_.Exception.Response.StatusCode
    $bodyText = $null
    try { $reader = New-Object IO.StreamReader($_.Exception.Response.GetResponseStream()); $bodyText=$reader.ReadToEnd(); $reader.Dispose() } catch {}
    return @{ Code=$code; Json=($(if($bodyText){$bodyText | ConvertFrom-Json}else{$null})) }
  }
}
function Get-Jobs {
  $response = Invoke-RestMethod "$Orch/jobs?limit=200" -ErrorAction Stop
  if ($null -ne $response.PSObject.Properties['value']) { return @($response.value) }
  return @($response)
}
function Post-Code([string]$Url, $Obj, [hashtable]$Headers = @{}) {
  try {
    return [int](Invoke-WebRequest -Method Post -Uri $Url -Headers $Headers -ContentType "application/json" `
      -Body ($Obj | ConvertTo-Json -Depth 5) -UseBasicParsing -ErrorAction Stop).StatusCode
  } catch {
    if ($null -eq $_.Exception.Response) { throw }
    return [int]$_.Exception.Response.StatusCode
  }
}
function Count-JobsFor([string]$Sha) {
  $matchingJobs = @((Get-Jobs) | Where-Object { $_.commit_sha -eq $Sha })
  return [int]$matchingJobs.Count
}
function Wait-JobTerminal([string]$JobId, [int]$Seconds = 90) {
  $end = (Get-Date).AddSeconds($Seconds)
  while ((Get-Date) -lt $end) {
    $job = Invoke-RestMethod "$Orch/jobs/$JobId" -TimeoutSec 3 -ErrorAction Stop
    if ($job.status -in @("COMPLETED", "FAILED")) { return $job }
    Start-Sleep -Seconds 1
  }
  throw "Job $JobId did not reach COMPLETED/FAILED within $Seconds seconds"
}

Write-Host "`n== 1. Services and infrastructure ==" -ForegroundColor Cyan
Check "orchestrator health" { Assert ((Invoke-RestMethod "$Orch/health").status -eq "healthy") "not healthy" }
Check "webhook health" { Assert ((Invoke-RestMethod "$Webhook/healthz").status -eq "ok") "not healthy" }
Check "reviewer API health" { Invoke-RestMethod "$Reviewer/healthz" | Out-Null }
Check "gateway health" { Invoke-RestMethod "$Gateway/healthz" | Out-Null }
Check "Redis responds PONG" {
  $pong = (docker compose exec -T redis redis-cli ping 2>&1 | Out-String).Trim()
  Assert ($pong -eq "PONG") "Redis returned '$pong'"
}
Check "Celery worker is registered" {
  $answer = (docker compose exec -T review-worker celery -A app.worker.celery_app:celery_app inspect ping --timeout=8 2>&1 | Out-String)
  Assert ($answer -match "pong") "worker did not answer inspect ping: $answer"
}
Check "Celery worker registered fidelis.run_review task" {
  $registered = (docker compose exec -T review-worker celery -A app.worker.celery_app:celery_app inspect registered --timeout=8 2>&1 | Out-String)
  Assert ($registered -match "fidelis.run_review") "review task was not registered: $registered"
}

Write-Host "`n== 2. Event -> queue -> worker -> reviewer ==" -ForegroundColor Cyan
$sha1 = New-Sha
$body1 = New-Body "opened" $sha1
$delivery1 = "$Run-open"
$script:JobId = $null
Check "valid PR event accepted and job_id returned" {
  $r = Send-Webhook $delivery1 $body1
  Assert ($r.Code -eq 200) "HTTP $($r.Code)"
  Assert ($r.Json.status -eq "queued") "Webhook status '$($r.Json.status)'"
  Assert ($r.Json.job_id) "no job_id returned"
  $script:JobId = $r.Json.job_id
}
Check "job reaches the expected worker terminal state" {
  Assert ($script:JobId) "job_id unavailable from prior check"
  $j = Wait-JobTerminal $script:JobId 120
  Assert ($j.status -eq $ExpectedFinalStatus) "final status $($j.status), expected $ExpectedFinalStatus, error=$($j.error_message)"
  Assert ($j.started_at) "started_at was not recorded"
  Assert ($j.completed_at) "completed_at was not recorded"
  if ($ExpectedFinalStatus -eq "FAILED") { Assert ($j.error_message) "FAILED job did not record an error" }
}
Check "job record and final state are durable in PostgreSQL" {
  $n = (docker compose exec -T postgres psql -U $PgUser -d $PgDb -t -A -c "select count(*) from review_jobs where id='$script:JobId' and status='$ExpectedFinalStatus' and started_at is not null and completed_at is not null" 2>&1 | Out-String).Trim()
  Assert ($n -eq "1") "database count/status query returned '$n'"
}
Check "Celery dispatch outbox is marked published" {
  $n = (docker compose exec -T postgres psql -U $PgUser -d $PgDb -t -A -c "select count(*) from review_job_outbox where job_id='$script:JobId' and published_at is not null" 2>&1 | Out-String).Trim()
  Assert ($n -eq "1") "outbox query returned '$n'"
}
Check "worker lifecycle endpoint rejects requests without its internal token" {
  $code = Post-Code "$Orch/jobs/$script:JobId/status" @{ event="started"; attempt=2 }
  Assert ($code -eq 401) "unauthenticated callback returned HTTP $code"
}
Check "terminal job cannot be re-entered" {
  $headers = @{ "X-Worker-Token" = $WorkerToken }
  $code = Post-Code "$Orch/jobs/$script:JobId/status" @{ event="started"; attempt=99 } $headers
  Assert ($code -eq 409) "terminal job re-entry returned HTTP $code, expected 409"
}

Write-Host "`n== 3. Duplicate safety ==" -ForegroundColor Cyan
Check "same delivery ID is ignored by Webhook" {
  $r = Send-Webhook $delivery1 $body1
  Assert ($r.Code -eq 200 -and $r.Json.status -eq "duplicate_ignored") "got HTTP $($r.Code), $($r.Json.status)"
  Assert ((Count-JobsFor $sha1) -eq 1) "duplicate delivery created another job"
}
if ($ExpectedFinalStatus -eq "FAILED") {
  # A FAILED job is intentionally excluded from the active-job uniqueness
  # rule so a later, new delivery can create a fresh retry job for the same SHA.
  Check "new delivery retries a previously FAILED commit with a new job" {
    $r = Send-Webhook "$Run-same-commit-retry" $body1
    Assert ($r.Code -eq 200) "HTTP $($r.Code)"
    Assert ($r.Json.job_id -and $r.Json.job_id -ne $script:JobId) "failed job was incorrectly reused"
    Assert ($r.Json.duplicate -eq $false) "retry was incorrectly marked duplicate"
    $retryJob = Wait-JobTerminal $r.Json.job_id 120
    Assert ($retryJob.status -eq "FAILED") "retry job ended $($retryJob.status), expected FAILED"
    Assert ((Count-JobsFor $sha1) -eq 2) "expected original failed job plus one retry job"
  }
} else {
  Check "new delivery ID for same commit reuses the existing job" {
    $r = Send-Webhook "$Run-same-commit" $body1
    Assert ($r.Code -eq 200) "HTTP $($r.Code)"
    Assert ($r.Json.job_id -eq $script:JobId) "job_id changed"
    Assert ($r.Json.duplicate -eq $true) "duplicate flag missing"
    Assert ((Count-JobsFor $sha1) -eq 1) "same commit produced multiple jobs"
  }
}

Write-Host "`n== 4. New commit creates a new execution ==" -ForegroundColor Cyan
$sha2 = New-Sha
Check "new commit on same PR receives a new job and reaches a terminal state" {
  $r = Send-Webhook "$Run-new-commit" (New-Body "synchronize" $sha2)
  Assert ($r.Code -eq 200 -and $r.Json.job_id) "job was not created: HTTP $($r.Code)"
  Assert ($r.Json.job_id -ne $script:JobId) "reused previous job ID"
  $j = Wait-JobTerminal $r.Json.job_id 120
  Assert ($j.status -eq $ExpectedFinalStatus) "new commit ended $($j.status), expected ${ExpectedFinalStatus}: $($j.error_message)"
  Assert ((Count-JobsFor $sha2) -eq 1) "expected exactly one job for new commit"
}

if ($RequireRetry) {
  Write-Host "`n== 5. Retry behavior ==" -ForegroundColor Cyan
  Check "flaky mock reviewer caused Celery retry(s) before completion" {
    $logs = (docker compose logs --no-color review-worker 2>&1 | Out-String)
    $retryLine = "job $($script:JobId): reviewer requested retry"
    Assert ($logs -match [regex]::Escape($retryLine)) "no retry for this job found; set REVIEWER_MOCK_SCENARIO=flaky and recreate reviewer + review-worker"
  }
}
if ($RequireFailure) {
  Write-Host "`n== 6. Permanent failure path ==" -ForegroundColor Cyan
  Check "permanent reviewer failure ends FAILED instead of retrying forever" {
    $j = Invoke-RestMethod "$Orch/jobs/$script:JobId" -ErrorAction Stop
    Assert ($j.status -eq "FAILED" -and $j.error_message) "job ended $($j.status): $($j.error_message)"
  }
}

Write-Host "`nWeek 3: $($script:Pass) passed, $($script:Fail) failed" -ForegroundColor $(if($script:Fail -eq 0){"Green"}else{"Red"})
if ($script:Fail -gt 0) { exit 1 }
