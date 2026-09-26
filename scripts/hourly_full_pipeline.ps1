<#
.SYNOPSIS
    Hourly full-refresh pipeline orchestrator (Phase 1 - manual run).

.DESCRIPTION
    Runs ONE complete data-refresh + full latest-report.md generation cycle
    with parallel independent branches, per-source status recording, cycle
    state JSON, atomic publication, and separated REPORT_READY vs PUBLISHED
    status. Designed to be scheduled hourly later; Phase 1 is manual-only.

    Branches (independent table ownership - no cross-branch write contention):
      A. equities + indicators      -> us_equities, us_equities_indicators
      B. macro markets              -> macro, macro_live (+ETF flow refresh inside)
      C. economic data              -> economic_* tables
      D. FINRA / short analytics    -> finra_*, short analytics
      E. news                       -> news_* tables
    JOIN -> rule_based_market_update.py (full report) -> atomic publish -> commit/push.

    Source severity (see scripts/pipeline_state.py):
      CRITICAL: us_equities, equity_indicators, macro_rates,
                macro_fx_commodities, macro_market_indices
      IMPORTANT: economic_releases, etf_flows, finra_short, news_fetch, news_signals
      OPTIONAL:  news_classify, asia_futures

    Exit codes:
      0 = REPORT_READY (publication may still be PUSH_FAILED - check state JSON)
      1 = FAILED (critical source failure or report generation failed)

.NOTES
    Mutex: Local\DBBuilderHourlyPipeline prevents overlapping cycles.
    news_classify exit 75 = Ollama unavailable -> DEGRADED (not a failure).
    macro_data_fetch has an Investing.com 403 circuit breaker (max 2 trips).
#>

param(
    [switch]$DryRun,
    [int]$MacroTimeoutSeconds = 540
)

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
$Root = $PSScriptRoot | Split-Path -Parent
$Python = 'C:\Users\User\anaconda3\envs\PostgreSQL_db\python.exe'
$LogDir = Join-Path $Root 'logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$StateDir = Join-Path $Root 'reports\state'
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null

$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$Stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$Log = Join-Path $LogDir ("hourly_pipeline_" + $Stamp + ".log")

# Cycle ID in HKT
$HktZone = [System.TimeZoneInfo]::FindSystemTimeZoneById('China Standard Time')
$NowHkt = [System.TimeZoneInfo]::ConvertTimeFromUtc([DateTime]::UtcNow, $HktZone)
$CycleId = $NowHkt.ToString('yyyyMMdd_HHmm') + '_HKT'
$CycleStart = Get-Date

function Write-Log($Level, $Message) {
    $Line = ('{0} [{1}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message)
    Add-Content -LiteralPath $Log -Value $Line
    Write-Host $Line
}

function New-ProcessInfo {
    param(
        [string]$Python,
        [string]$ScriptPath,
        [string]$WorkingDirectory,
        [string]$ExtraArgs = ''
    )
    # Create per-branch log file for output capture
    $scriptName = Split-Path $ScriptPath -LeafBase
    $logDir = Join-Path $WorkingDirectory 'reports\state\logs'
    if (-not (Test-Path $logDir)) {
        New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    }
    $branchLog = Join-Path $logDir ($scriptName + '.log')
    # Use cmd.exe to redirect stdout/stderr to file (prevents pipe deadlock)
    $cmdArgs = '/c "' + $Python + '" -u "' + $ScriptPath + '" ' + $ExtraArgs + ' > "' + $branchLog + '" 2>&1'
    $pinfo = New-Object System.Diagnostics.ProcessStartInfo
    $pinfo.FileName = 'cmd.exe'
    $pinfo.Arguments = $cmdArgs
    $pinfo.WorkingDirectory = $WorkingDirectory
    $pinfo.UseShellExecute = $false
    $pinfo.CreateNoWindow = $true
    # Forward critical environment variables explicitly
    $pinfo.EnvironmentVariables['PYTHONUNBUFFERED'] = '1'
    $pinfo.EnvironmentVariables['PYTHONPATH'] = Join-Path $WorkingDirectory 'src;' + (Join-Path $WorkingDirectory 'scripts')
    # Forward database connection environment variables
    $envVars = @('LOCAL_DATABASE_URL','LOCAL_DB_HOST','LOCAL_DB_PORT','LOCAL_DB_NAME','LOCAL_DB_USER','LOCAL_DB_PASSWORD','LOCAL_ENV_DIR','DB_BUILDER_ENV_DIR','NEON_DATABASE_URL','NEON_DB_HOST','NEON_DB_NAME','NEON_DB_USER','NEON_DB_PASSWORD')
    foreach ($var in $envVars) {
        $val = [System.Environment]::GetEnvironmentVariable($var)
        if (-not [string]::IsNullOrWhiteSpace($val)) {
            $pinfo.EnvironmentVariables[$var] = $val
        }
    }
    return $pinfo, $branchLog
}

function Invoke-PythonStep {
    param(
        [string]$Script,
        [string]$Arguments = '',
        [int]$TimeoutSeconds = 600,
        [int]$MaxAttempts = 1,
        [string]$StepName
    )
    for ($Attempt = 1; $Attempt -le $MaxAttempts; $Attempt++) {
        Write-Log 'START' ("$StepName attempt $Attempt")
        $Out = Join-Path $env:TEMP ([guid]::NewGuid().ToString() + '.out.log')
        $Err = Join-Path $env:TEMP ([guid]::NewGuid().ToString() + '.err.log')
        $Cmd = ('/c ""{0}" "{1}" {2} > "{3}" 2> "{4}"' -f $Python, (Join-Path $Root $Script), $Arguments, $Out, $Err)
        $Psi = [System.Diagnostics.ProcessStartInfo]::new()
        $Psi.FileName = 'cmd.exe'
        $Psi.Arguments = $Cmd
        $Psi.WorkingDirectory = $Root
        $Psi.UseShellExecute = $false
        $Psi.EnvironmentVariables['PYTHONUNBUFFERED'] = '1'
        $Psi.EnvironmentVariables['PYTHONPATH'] = Join-Path $Root 'src;' + (Join-Path $Root 'scripts')
        $envVars = @('LOCAL_DATABASE_URL','LOCAL_DB_HOST','LOCAL_DB_PORT','LOCAL_DB_NAME','LOCAL_DB_USER','LOCAL_DB_PASSWORD','NEON_DATABASE_URL','NEON_DB_HOST','NEON_DB_NAME')
        foreach ($var in $envVars) {
            $val = [System.Environment]::GetEnvironmentVariable($var)
            if (-not [string]::IsNullOrWhiteSpace($val)) {
                $Psi.EnvironmentVariables[$var] = $val
            }
        }
        $P = [System.Diagnostics.Process]::Start($Psi)
        $TimeoutMs = [Math]::Max(1, $TimeoutSeconds) * 1000
        if (-not $P.WaitForExit($TimeoutMs)) {
            $null = & taskkill.exe /PID $P.Id /T /F 2>&1
            Write-Log 'TIMEOUT' ("$StepName exceeded $TimeoutSeconds seconds")
            if (Test-Path $Out) { Get-Content $Out | Add-Content -LiteralPath $Log }
            if (Test-Path $Err) { Get-Content $Err | Add-Content -LiteralPath $Log }
            if ($Attempt -lt $MaxAttempts) { Start-Sleep -Seconds 30; continue }
            return @{ Ok = $false; ExitCode = -1; Output = 'TIMEOUT'; Error = $null }
        }
        $P.WaitForExit()
        $ExitCode = $P.ExitCode
        if (Test-Path $Out) { Get-Content $Out | Add-Content -LiteralPath $Log }
        if (Test-Path $Err) { Get-Content $Err | Add-Content -LiteralPath $Log }
        if ($ExitCode -eq 0) {
            Write-Log 'SUCCESS' $StepName
            return @{ Ok = $true; ExitCode = 0; Output = (Get-Content $Out -Raw -ErrorAction SilentlyContinue); Error = $null }
        }
        Write-Log 'ERROR' ("$StepName exited with code $ExitCode")
        if ($Attempt -lt $MaxAttempts) { Start-Sleep -Seconds 30; continue }
        return @{ Ok = $false; ExitCode = $ExitCode; Output = (Get-Content $Out -Raw -ErrorAction SilentlyContinue); Error = (Get-Content $Err -Raw -ErrorAction SilentlyContinue) }
    }
}

# ---------------------------------------------------------------------------
# Mutex: never overlap cycles
# ---------------------------------------------------------------------------
$Mutex = [System.Threading.Mutex]::new($false, 'Local\DBBuilderHourlyPipeline')
$HasMutex = $false
try {
    try { $HasMutex = $Mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $HasMutex = $true }
    if (-not $HasMutex) {
        Write-Log 'SKIP' 'Another hourly pipeline cycle is already running'
        exit 1
    }

    Write-Log 'START' ("Hourly full-refresh pipeline cycle $CycleId" + $(if ($DryRun) { ' [DRY RUN]' } else { '' }))

    # -----------------------------------------------------------------------
    # State: RUNNING
    # -----------------------------------------------------------------------
    $StateJson = Join-Path $StateDir 'latest_report_status.json'
    $PrevState = $null
    if (Test-Path $StateJson) {
        try { $PrevState = Get-Content $StateJson -Raw | ConvertFrom-Json } catch {}
    }
    $SourceMetaFile = Join-Path $StateDir ("cycle_" + $CycleId + "_sources.json")

    # -----------------------------------------------------------------------
    # PARALLEL BRANCHES (independent table ownership)
    # -----------------------------------------------------------------------
    Write-Log 'BRANCHES' 'Starting parallel data-refresh branches A-E'

    $BranchJobs = @()

    # Branch A: equities + indicators (us_equities, us_equities_indicators)
    # Session-aware refresh: pgSQL_equities_auto.py checks the trading calendar
    # and local DB before fetching. If the latest completed NYSE session is already
    # stored and indicators are current, the script exits in seconds.
    # Historical gap repair (repair_historical_equity_gaps.py) is moved to a
    # separate maintenance workflow, not the hourly critical path.
    $BranchJobs += Start-Job -Name 'BranchA_Equities' -ScriptBlock {
        param($Root, $Python, $Log, $DryRun)
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        if ($DryRun) {
            $sw.Stop()
            $s1 = Test-Path (Join-Path $Root 'scripts\pgSQL_equities_auto.py')
            [pscustomobject]@{ Branch='A_equities'; Ok=$s1; Seconds=$sw.Elapsed.TotalSeconds; ExitCode=0; Error=$null; Output=('DRY RUN: pgSQL_equities_auto.py validated (session-aware)') }
            return
        }
        $eqOk = $false
        for ($attempt = 1; $attempt -le 3; $attempt++) {
            $pinfo, $branchLog = New-ProcessInfo -Python $Python -ScriptPath (Join-Path $Root 'scripts\pgSQL_equities_auto.py') -WorkingDirectory $Root
            $p = [System.Diagnostics.Process]::Start($pinfo)
            if (-not $p.WaitForExit(300000)) {
                # Kill the cmd.exe process tree to terminate the Python child
                $null = taskkill.exe /F /T /PID $p.Id 2>&1
                $p.Kill() 2>$null
                $sw.Stop()
                $diagOutput = if (Test-Path $branchLog) { Get-Content $branchLog -Raw -ErrorAction SilentlyContinue } else { 'No log file' }
                return [pscustomobject]@{ Branch='A_equities'; Ok=$false; Seconds=$sw.Elapsed.TotalSeconds; Output=('TIMEOUT after 300s (pgSQL_equities_auto) | Last: ' + $diagOutput); ExitCode=-1; Error=$null }
            }
            $attemptOutput = if (Test-Path $branchLog) { Get-Content $branchLog -Raw -ErrorAction SilentlyContinue } else { '' }
            if ($p.ExitCode -eq 0) { $eqOk = $true; break }
            if ($attempt -lt 3) { Start-Sleep -Seconds 60 }
        }
        if (-not $eqOk) {
            $sw.Stop()
            $failOutput = if (Test-Path (Join-Path $Root 'reports\state\logs\pgSQL_equities_auto.log')) { Get-Content (Join-Path $Root 'reports\state\logs\pgSQL_equities_auto.log') -Raw -ErrorAction SilentlyContinue } else { 'No log' }
            return [pscustomobject]@{ Branch='A_equities'; Ok=$false; Seconds=$sw.Elapsed.TotalSeconds; Output=('FAIL: pgSQL_equities_auto failed after 3 attempts | ' + $failOutput); ExitCode=-1; Error=$null }
        }
        $sw.Stop()
        [pscustomobject]@{ Branch='A_equities'; Ok=$true; Seconds=$sw.Elapsed.TotalSeconds; ExitCode=0; Error=$null; Output=$attemptOutput }
    } -ArgumentList $Root, $Python, $Log, $DryRun

    # Branch B: macro markets (macro, macro_live) — includes ETF flow refresh
    $BranchJobs += Start-Job -Name 'BranchB_Macro' -ScriptBlock {
        param($Root, $Python, $MacroTimeoutSeconds, $DryRun)
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        if ($DryRun) {
            $sw.Stop()
            $s1 = Test-Path (Join-Path $Root 'scripts\macro_data_fetch.py')
            [pscustomobject]@{ Branch='B_macro'; Ok=$s1; Seconds=$sw.Elapsed.TotalSeconds; ExitCode=0; Error=$null; Output=('DRY RUN: macro script validated') }
            return
        }
        $pinfo, $branchLog = New-ProcessInfo -Python $Python -ScriptPath (Join-Path $Root 'scripts\macro_data_fetch.py') -WorkingDirectory $Root
        $p = [System.Diagnostics.Process]::Start($pinfo)
        if (-not $p.WaitForExit($MacroTimeoutSeconds * 1000)) {
            # Kill the cmd.exe process tree to terminate the Python child
            $null = taskkill.exe /F /T /PID $p.Id 2>&1
            $p.Kill() 2>$null; $sw.Stop()
            $diagOutput = if (Test-Path $branchLog) { Get-Content $branchLog -Raw -ErrorAction SilentlyContinue } else { 'No log file' }
            return [pscustomobject]@{ Branch='B_macro'; Ok=$false; Seconds=$sw.Elapsed.TotalSeconds; Output=('TIMEOUT after ' + $MacroTimeoutSeconds + 's | Last: ' + $diagOutput); ExitCode=-1; Error=$null }
        }
        $out = if (Test-Path $branchLog) { Get-Content $branchLog -Raw -ErrorAction SilentlyContinue } else { '' }
        $sw.Stop()
        [pscustomobject]@{ Branch='B_macro'; Ok=($p.ExitCode -eq 0); Seconds=$sw.Elapsed.TotalSeconds; Output=$out; ExitCode=$p.ExitCode; Error=$null }
    } -ArgumentList $Root, $Python, $MacroTimeoutSeconds, $DryRun

    # Branch C: economic data (economic_* tables) — natural cadence: check for new releases
    $BranchJobs += Start-Job -Name 'BranchC_Economic' -ScriptBlock {
        param($Root, $Python, $DryRun)
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        if ($DryRun) {
            $sw.Stop()
            $s1 = Test-Path (Join-Path $Root 'scripts\economic_data_fetch.py')
            $s2 = Test-Path (Join-Path $Root 'scripts\global_economic_data_fetch.py')
            [pscustomobject]@{ Branch='C_economic'; Ok=($s1 -and $s2); Seconds=$sw.Elapsed.TotalSeconds; ExitCode=0; Error=$null; Output=('DRY RUN: economic scripts validated') }
            return
        }
        $r1 = & $Python (Join-Path $Root 'scripts\economic_data_fetch.py') --upsert-local 2>&1
        $r2 = & $Python (Join-Path $Root 'scripts\global_economic_data_fetch.py') --upsert-local 2>&1
        $sw.Stop()
        [pscustomobject]@{ Branch='C_economic'; Ok=($LASTEXITCODE -eq 0); Seconds=$sw.Elapsed.TotalSeconds; ExitCode=$LASTEXITCODE; Error=$null; Output=($r1 + $r2 | Out-String) }
    } -ArgumentList $Root, $Python, $DryRun

    # Branch D: FINRA / short analytics (finra_*, short analytics)
    $BranchJobs += Start-Job -Name 'BranchD_Finra' -ScriptBlock {
        param($Root, $Python, $DryRun)
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        if ($DryRun) {
            $sw.Stop()
            $s1 = Test-Path (Join-Path $Root 'scripts\finra_short_incremental.py')
            $s2 = Test-Path (Join-Path $Root 'scripts\short_analytics_latest_sync.py')
            [pscustomobject]@{ Branch='D_finra'; Ok=($s1 -and $s2); Seconds=$sw.Elapsed.TotalSeconds; ExitCode=0; Error=$null; Output=('DRY RUN: FINRA scripts validated') }
            return
        }
        $r1 = & $Python (Join-Path $Root 'scripts\finra_short_incremental.py') 2>&1
        $r2 = & $Python (Join-Path $Root 'scripts\short_analytics_latest_sync.py') 2>&1
        $sw.Stop()
        [pscustomobject]@{ Branch='D_finra'; Ok=($LASTEXITCODE -eq 0); Seconds=$sw.Elapsed.TotalSeconds; ExitCode=$LASTEXITCODE; Error=$null; Output=($r1 + $r2 | Out-String) }
    } -ArgumentList $Root, $Python, $DryRun

    # Branch E: news (news_* tables) — classify has Ollama circuit breaker (exit 75 = DEGRADED)
    $BranchJobs += Start-Job -Name 'BranchE_News' -ScriptBlock {
        param($Root, $Python, $DryRun)
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        if ($DryRun) {
            $sw.Stop()
            $s1 = Test-Path (Join-Path $Root 'scripts\news_fetch.py')
            $s2 = Test-Path (Join-Path $Root 'scripts\news_classify.py')
            $s3 = Test-Path (Join-Path $Root 'scripts\news_signals.py')
            [pscustomobject]@{ Branch='E_news'; Ok=($s1 -and $s2 -and $s3); Seconds=$sw.Elapsed.TotalSeconds; ExitCode=0; Error=$null; ClassifyDegraded=$false; Output=('DRY RUN: news scripts validated') }
            return
        }
        $r1 = & $Python (Join-Path $Root 'scripts\news_fetch.py') --upsert-local --limit 200 2>&1
        $newsFetchOk = ($LASTEXITCODE -eq 0)
        $r2 = & $Python (Join-Path $Root 'scripts\news_classify.py') --upsert-local --limit 100 --max-seconds 300 2>&1
        $classifyExit = $LASTEXITCODE
        $r3 = & $Python (Join-Path $Root 'scripts\news_signals.py') --upsert-local --all-windows 2>&1
        $signalsOk = ($LASTEXITCODE -eq 0)
        $sw.Stop()
        # classify exit 75 = Ollama unavailable -> DEGRADED not FAILED
        $classifyDegraded = ($classifyExit -eq 75)
        $ok = $newsFetchOk -and $signalsOk -and ($classifyExit -eq 0 -or $classifyDegraded)
        [pscustomobject]@{ Branch='E_news'; Ok=$ok; Seconds=$sw.Elapsed.TotalSeconds; ExitCode=$LASTEXITCODE; Error=$null; ClassifyDegraded=$classifyDegraded; Output=($r1 + $r2 + $r3 | Out-String) }
    } -ArgumentList $Root, $Python, $DryRun

    # -----------------------------------------------------------------------
    # -----------------------------------------------------------------------
    # JOIN: global timeout — all branches run concurrently, share one window
    # -----------------------------------------------------------------------
    Write-Log 'JOIN' 'Waiting for all branches to complete...'
    $Jobs = @($BranchJobs)
    $OverallTimeoutSeconds = 600
    $JoinStopwatch = [System.Diagnostics.Stopwatch]::StartNew()

    # Global wait: all jobs share one timeout window
    $null = Wait-Job -Job $Jobs -Timeout $OverallTimeoutSeconds
    $JoinStopwatch.Stop()
    $JoinElapsed = [math]::Round($JoinStopwatch.Elapsed.TotalSeconds, 1)
    Write-Log 'JOIN' "Global wait complete after $JoinElapsed s"

    $BranchResults = @()
    foreach ($Job in $Jobs) {
        $JobState = $Job.JobStateInfo.State

        # Capture error stream from background job
        $JobError = $null
        if ($Job.ChildJobs.Count -gt 0) {
            $JobError = $Job.ChildJobs[0].Error | Out-String
        }

        if ($JobState -eq 'Running') {
            # Timed out — kill child process tree first, then stop job
            # Get the actual process PID from the child job
            $JobPid = $Job.ChildJobs[0].Id
            $null = taskkill.exe /F /T /PID $JobPid 2>&1
            $null = Stop-Job -Job $Job -ErrorAction SilentlyContinue
            $Result = [pscustomobject]@{
                Name = $Job.Name
                Status = 'TIMEOUT'
                Seconds = [double]$OverallTimeoutSeconds
                ExitCode = [int]-1
                Error = $JobError
                Output = "TIMEOUT after ${OverallTimeoutSeconds}s (global)"
                Branch = $Job.Name
                Ok = [bool]$false
            }
            Write-Log 'TIMEOUT' ("{0} exceeded global timeout {1}s" -f $Job.Name, $OverallTimeoutSeconds)
        } else {
            # Completed — receive output (wrapped to ensure cleanup)
            $RawResult = $null
            try {
                $RawResult = Receive-Job -Job $Job
            } finally {
                # Ensure job is cleaned up even if Receive-Job throws
                $CleanupJobId = $Job.Id
                $CleanupExisting = Get-Job -Id $CleanupJobId -ErrorAction SilentlyContinue
                if ($null -ne $CleanupExisting -and $CleanupExisting.State -ne 'Running') {
                    Remove-Job -Job $CleanupExisting -Force -ErrorAction SilentlyContinue
                }
            }
            if ($null -eq $RawResult) {
                $Result = [pscustomobject]@{
                    Name = $Job.Name
                    Status = 'FAILED'
                    Seconds = [double]0
                    ExitCode = [int]-1
                    Error = $JobError
                    Output = ('JOB RETURNED NOTHING (state: ' + $JobState + ')' + $(if ($JobError) { " | ERROR: " + $JobError } else { '' }))
                    Branch = $Job.Name
                    Ok = [bool]$false
                }
            } else {
                # Normalize: compute all values BEFORE constructing [pscustomobject]
                # PowerShell 5.1 does not support inline if expressions inside object literals
                if ($RawResult -isnot [pscustomobject]) {
                    $RawResult = [pscustomobject]$RawResult
                }

                # Pre-compute normalized values
                $NormalizedName = [string]$RawResult.Branch
                $NormalizedStatus = if ($RawResult.Ok) { 'OK' } else { 'FAILED' }
                $NormalizedSeconds = [double]0
                if ($null -ne $RawResult -and
                    $RawResult.PSObject.Properties.Name -contains 'Seconds' -and
                    $null -ne $RawResult.Seconds) {
                    $NormalizedSeconds = [double]$RawResult.Seconds
                }
                $NormalizedExitCode = [int]0
                if ($null -ne $RawResult -and
                    $RawResult.PSObject.Properties.Name -contains 'ExitCode' -and
                    $null -ne $RawResult.ExitCode) {
                    $NormalizedExitCode = [int]$RawResult.ExitCode
                }
                $NormalizedError = if ($JobError) { $JobError } else { $null }
                if ($null -eq $NormalizedError -and
                    $RawResult.PSObject.Properties.Name -contains 'Error' -and
                    $null -ne $RawResult.Error) {
                    $NormalizedError = $RawResult.Error
                }
                $NormalizedBranch = [string]$RawResult.Branch
                $NormalizedOk = [bool]$false
                if ($RawResult.PSObject.Properties.Name -contains 'Ok') {
                    $NormalizedOk = [bool]$RawResult.Ok
                }

                $Result = [pscustomobject]@{
                    Name     = $NormalizedName
                    Status   = $NormalizedStatus
                    Seconds  = $NormalizedSeconds
                    ExitCode = $NormalizedExitCode
                    Error    = $NormalizedError
                    Branch   = $NormalizedBranch
                    Ok       = $NormalizedOk
                }
            }
        }

        # Ensure all required schema fields are always present (PSCustomObject-compatible)
        if (-not ($Result.PSObject.Properties.Name -contains 'Name')) {
            $Result = $Result | Add-Member -NotePropertyName 'Name' -NotePropertyValue $Job.Name -PassThru
        }
        if (-not ($Result.PSObject.Properties.Name -contains 'Status')) {
            $Result = $Result | Add-Member -NotePropertyName 'Status' -NotePropertyValue 'UNKNOWN' -PassThru
        }
        if (-not ($Result.PSObject.Properties.Name -contains 'Seconds')) {
            $Result = $Result | Add-Member -NotePropertyName 'Seconds' -NotePropertyValue ([double]0) -PassThru
        }
        if (-not ($Result.PSObject.Properties.Name -contains 'ExitCode')) {
            $Result = $Result | Add-Member -NotePropertyName 'ExitCode' -NotePropertyValue ([int]-1) -PassThru
        }
        if (-not ($Result.PSObject.Properties.Name -contains 'Error')) {
            $Result = $Result | Add-Member -NotePropertyName 'Error' -NotePropertyValue $null -PassThru
        }
        if (-not ($Result.PSObject.Properties.Name -contains 'Branch')) {
            $Result = $Result | Add-Member -NotePropertyName 'Branch' -NotePropertyValue $Job.Name -PassThru
        }

        # Cleanup: remove job immediately (already stopped or completed)
        try {
            $JobId = $Job.Id
            $ExistingJob = Get-Job -Id $JobId -ErrorAction SilentlyContinue
            if ($null -ne $ExistingJob) {
                # Remove immediately without re-checking state
                $null = Stop-Job -Job $ExistingJob -ErrorAction SilentlyContinue
                Remove-Job -Job $ExistingJob -Force -ErrorAction SilentlyContinue
            }
        } catch {
            # Cleanup failure must not change branch result status
            Write-Log 'WARN' ("Cleanup failed for {0}: {1}" -f $Job.Name, $_.Exception.Message)
        }
        $BranchResults += $Result
        $Status = if ($Result.Ok) { 'OK' } else { 'FAIL' }
        Write-Log 'BRANCH' ("{0}: {1} ({2:N0}s)" -f $Result.Branch, $Status, $Result.Seconds)
        if ($Result.Output) {
            $Result.Output | Out-String -Width 4096 | ForEach-Object { $_.TrimEnd() } | Where-Object { $_ } | Add-Content -LiteralPath $Log
        }
        if ($Result.Error) {
            Write-Log 'ERROR' ("{0} error captured" -f $Result.Branch)
        }
    }
    $BranchRuntime = if ($BranchResults.Count -gt 0) { ($BranchResults | Measure-Object -Property Seconds -Maximum).Maximum } else { 0 }

# --- Post-loop cleanup: ensure no lingering processes ---
# Kill any remaining Start-Job processes from this pipeline
$null = Get-Job | Remove-Job -Force -ErrorAction SilentlyContinue

Write-Log 'BRANCHES' ("All branches complete. Max branch runtime: {0:N0}s" -f $BranchRuntime)
    Write-Log 'BRANCHES' ("All branches complete. Max branch runtime: {0:N0}s" -f $BranchRuntime)

    # -----------------------------------------------------------------------
    # Map branch results -> source statuses (with DB-derived observation dates)
    # -----------------------------------------------------------------------
    $BranchA = $BranchResults | Where-Object { $_.Branch -eq 'A_equities' }
    $BranchB = $BranchResults | Where-Object { $_.Branch -eq 'B_macro' }
    $BranchC = $BranchResults | Where-Object { $_.Branch -eq 'C_economic' }
    $BranchD = $BranchResults | Where-Object { $_.Branch -eq 'D_finra' }
    $BranchE = $BranchResults | Where-Object { $_.Branch -eq 'E_news' }

    # Macro branch: PARTIAL (exit 1 with coverage satisfied) still usable for rates/FX
    $MacroPartial = ($BranchB.Ok -eq $false -and $BranchB.ExitCode -eq 1 -and $BranchB.Output -match 'coverage satisfied')

    $SourceMeta = @{
        cycle_id = $CycleId
        data_refresh_completed_at = (Get-Date).ToString('yyyy-MM-ddTHH:mm:ss+08:00')
        sources = @{}
        degraded_sources = @()
    }

    function Add-Source($Name, $Ok, $ObservationDate, $Note) {
        $Status = if ($Ok) { 'OK' } else { 'FAILED' }
        $SourceMeta.sources[$Name] = @{
            status = $Status
            observation_date = $ObservationDate
            retrieved_at = (Get-Date).ToString('yyyy-MM-ddTHH:mm:ss+08:00')
        }
        if ($Note) { $SourceMeta.sources[$Name].note = $Note }
    }

    Add-Source 'us_equities' ($BranchA.Ok) $null $null
    Add-Source 'equity_indicators' ($BranchA.Ok) $null $null
    Add-Source 'macro_rates' ($BranchB.Ok -or $MacroPartial) $null $null
    Add-Source 'macro_fx_commodities' ($BranchB.Ok -or $MacroPartial) $null $null
    Add-Source 'macro_market_indices' ($BranchB.Ok -or $MacroPartial) $null $null
    Add-Source 'economic_releases' ($BranchC.Ok) $null 'natural cadence: latest official release'
    Add-Source 'etf_flows' ($BranchB.Ok -or $MacroPartial) $null 'refreshed inside macro branch'
    Add-Source 'finra_short' ($BranchD.Ok) $null 'natural cadence: bi-weekly settlement'
    Add-Source 'news_fetch' ($BranchE.Ok) $null $null
    Add-Source 'news_signals' ($BranchE.Ok) $null $null
    Add-Source 'news_classify' ($true) $null $(if ($BranchE.ClassifyDegraded) { 'Ollama unavailable this cycle (exit 75) - DEGRADED' } else { 'classified normally' })
    Add-Source 'asia_futures' ($true) $null 'Investing.com primary; yfinance cash-index proxy fallback active'

    if ($BranchE.ClassifyDegraded) { $SourceMeta.degraded_sources += 'news_classify' }
    if ($MacroPartial) { $SourceMeta.degraded_sources += 'macro_partial_coverage' }

    # Persist source meta for the report generator
    $SourceMeta | ConvertTo-Json -Depth 5 | ForEach-Object { [System.IO.File]::WriteAllText($SourceMetaFile, $_, $Utf8NoBom) }
    Write-Log 'STATE' "Source metadata written: $SourceMetaFile"

    # -----------------------------------------------------------------------
    # CRITICAL source gate: proceed only if all CRITICAL sources OK
    # -----------------------------------------------------------------------
    $CriticalFailed = @('us_equities','equity_indicators','macro_rates','macro_fx_commodities','macro_market_indices') |
        Where-Object { $SourceMeta.sources[$_].status -ne 'OK' }
    if ($CriticalFailed) {
        Write-Log 'FAIL' ("Critical sources failed: " + ($CriticalFailed -join ', '))
        # Write FAILED state
        $FailState = @{
            cycle_id = $CycleId
            started_at = $CycleStart.ToString('yyyy-MM-ddTHH:mm:ss+08:00')
            completed_at = (Get-Date).ToString('yyyy-MM-ddTHH:mm:ss+08:00')
            overall_status = 'FAILED'
            publication_status = 'NOT_PUBLISHED'
            consecutive_failures = $(if ($PrevState -and $PrevState.overall_status -eq 'FAILED') { [int]$PrevState.consecutive_failures + 1 } else { 1 })
            failure_reason = ('critical sources failed: ' + ($CriticalFailed -join ', '))
            sources = $SourceMeta.sources
            degraded_sources = $SourceMeta.degraded_sources
            latest_report = @{ generated_at = $null; path = $null; sha256 = $null; cycle_id = $CycleId }
            git = @{ commit = $null; pushed = $false; head_equals_origin = $null; error = $null }
        }
        $FailState | ConvertTo-Json -Depth 5 | ForEach-Object { [System.IO.File]::WriteAllText($StateJson, $_, $Utf8NoBom) }
        exit 1
    }

    # -----------------------------------------------------------------------
    # FULL REPORT GENERATION (all sections preserved)
    # -----------------------------------------------------------------------
    Write-Log 'REPORT' 'Generating FULL rule-based market update report...'
    $ReportArgs = ('--save --window-hours 24 --publish-dashboard --cycle-id {0} --source-meta-json "{1}"' -f $CycleId, $SourceMetaFile)
    if (-not $DryRun) { $ReportArgs += ' --push-dashboard' }
    else { Write-Log 'DRYRUN' 'Push disabled for dry run' }
    $ReportResult = Invoke-PythonStep -Script 'scripts\rule_based_market_update.py' -Arguments $ReportArgs -TimeoutSeconds 900 -StepName 'rule_based_market_update.py'
    if (-not $ReportResult.Ok) {
        Write-Log 'FAIL' ("Report generation failed - ExitCode: {0}" -f $ReportResult.ExitCode)
        if ($ReportResult.Error) {
            Write-Log 'STDERR' ("Python stderr: {0}" -f ($ReportResult.Error -join [Environment]::NewLine))
        }
        if ($ReportResult.Output) {
            Write-Log 'STDOUT' ("Python stdout: {0}" -f ($ReportResult.Output -split [Environment]::NewLine | Select-Object -Last 20))
        }
        $FailState = @{
            cycle_id = $CycleId
            started_at = $CycleStart.ToString('yyyy-MM-ddTHH:mm:ss+08:00')
            completed_at = (Get-Date).ToString('yyyy-MM-ddTHH:mm:ss+08:00')
            overall_status = 'FAILED'
            publication_status = 'NOT_PUBLISHED'
            consecutive_failures = $(if ($PrevState -and $PrevState.overall_status -eq 'FAILED') { [int]$PrevState.consecutive_failures + 1 } else { 1 })
            failure_reason = 'report generation failed'
            sources = $SourceMeta.sources
            degraded_sources = $SourceMeta.degraded_sources
            latest_report = @{ generated_at = $null; path = $null; sha256 = $null; cycle_id = $CycleId }
            git = @{ commit = $null; pushed = $false; head_equals_origin = $null; error = $null }
        }
        $FailState | ConvertTo-Json -Depth 5 | ForEach-Object { [System.IO.File]::WriteAllText($StateJson, $_, $Utf8NoBom) }
        exit 1
    }

    # -----------------------------------------------------------------------
    # Post-report: compute SHA, verify, write FINAL state
    # -----------------------------------------------------------------------
    $DashboardReport = Join-Path $Root 'market-dashboard\data\latest-report.md'
    $ReportSha = $null
    $CommitHash = $null
    $Pushed = $false
    $HeadEqualsOrigin = $null
    $GitError = $null

    if (Test-Path $DashboardReport) {
        $ReportSha = (Get-FileHash -LiteralPath $DashboardReport -Algorithm SHA256).Hash.ToLower()
    }

    if (-not $DryRun) {
        # Commit/push was attempted inside rule_based_market_update.py; inspect git state
        try {
            $CommitHash = & git -C (Join-Path $Root 'market-dashboard') rev-parse HEAD 2>$null
            if ($LASTEXITCODE -ne 0) { $CommitHash = $null }
        } catch { $CommitHash = $null }
        try {
            & git -C (Join-Path $Root 'market-dashboard') fetch origin main 2>$null
            if ($LASTEXITCODE -eq 0) {
                $LocalHead = & git -C (Join-Path $Root 'market-dashboard') rev-parse HEAD
                $OriginHead = & git -C (Join-Path $Root 'market-dashboard') rev-parse origin/main
                $HeadEqualsOrigin = ($LocalHead -eq $OriginHead)
                # Pushed if HEAD is on origin (commit reachable from origin/main)
                if ($HeadEqualsOrigin) { $Pushed = $true }
                else {
                    # Check if HEAD commit is behind origin (rebase needed) or ahead (push failed)
                    $AheadCount = & git -C (Join-Path $Root 'market-dashboard') rev-list --count origin/main..HEAD
                    if ([int]$AheadCount -gt 0) { $Pushed = $false; $GitError = 'push failed or not attempted; local is ahead of origin' }
                }
            } else { $GitError = 'git fetch failed - cannot verify publication' }
        } catch { $GitError = $_.Exception.Message }
    }

    $PubStatus = if ($DryRun) { 'NOT_PUBLISHED' }
                 elseif ($null -eq $CommitHash) { 'COMMIT_FAILED' }
                 elseif ($Pushed -and $HeadEqualsOrigin) { 'PUSHED' }
                 else { 'PUSH_FAILED' }

    $FinalState = @{
        cycle_id = $CycleId
        started_at = $CycleStart.ToString('yyyy-MM-ddTHH:mm:ss+08:00')
        completed_at = (Get-Date).ToString('yyyy-MM-ddTHH:mm:ss+08:00')
        overall_status = 'REPORT_READY'
        publication_status = $PubStatus
        consecutive_failures = 0
        branch_runtimes = ($BranchResults | ForEach-Object { @{ branch = $_.Branch; seconds = [math]::Round($_.Seconds, 1); ok = $_.Ok } })
        total_runtime_seconds = [math]::Round(((Get-Date) - $CycleStart).TotalSeconds, 1)
        sources = $SourceMeta.sources
        degraded_sources = $SourceMeta.degraded_sources
        latest_report = @{
            generated_at = (Get-Date).ToString('yyyy-MM-ddTHH:mm:ss+08:00')
            path = $DashboardReport
            sha256 = $ReportSha
            cycle_id = $CycleId
        }
        git = @{
            commit = $CommitHash
            pushed = $Pushed
            head_equals_origin = $HeadEqualsOrigin
            error = $GitError
        }
    }
    $FinalState | ConvertTo-Json -Depth 6 | ForEach-Object { [System.IO.File]::WriteAllText($StateJson, $_, $Utf8NoBom) }
    Write-Log 'STATE' "Final state written: $StateJson (REPORT_READY / $PubStatus)"

    $TotalMin = [math]::Round(((Get-Date) - $CycleStart).TotalMinutes, 1)
    Write-Log 'SUCCESS' ("Cycle $CycleId complete in $TotalMin minutes. Status: REPORT_READY / $PubStatus")
    exit 0

} finally {
    if ($HasMutex) { $Mutex.ReleaseMutex() }
    $Mutex.Dispose()
}
