$ErrorActionPreference = 'Continue'
$Root = 'C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder'
$Python = 'C:\Users\User\anaconda3\envs\PostgreSQL_db\python.exe'
$LogDir = Join-Path $Root 'logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$Log = Join-Path $LogDir ('auto_macro_db_' + $Stamp + '.log')

function Write-Log($Level, $Message) {
    $Line = ('{0} [{1}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message)
    Add-Content -LiteralPath $Log -Value $Line
    Write-Host $Line
}

function Get-BlobStatus($RelPath) {
    $Full = Join-Path $Root $RelPath
    if (-not (Test-Path -LiteralPath $Full)) { return 'MISSING' }
    $HeadBlob = (& git -C $Root rev-parse ('HEAD:' + $RelPath) 2>$null)
    if (-not $HeadBlob) { return 'NOBLOB' }
    & git -C $Root diff --quiet --ignore-cr-at-eol HEAD -- $RelPath 2>$null
    $DiffCode = $LASTEXITCODE
    if ($DiffCode -eq 0) { return 'SYNC' }
    return 'LAG'
}

$VersionCommit = (& git -C $Root rev-parse HEAD 2>$null)
if (-not $VersionCommit) { $VersionCommit = 'UNKNOWN' }
Write-Log 'VERSION' ('commit=' + $VersionCommit + ' macro_data_fetch=' + (Get-BlobStatus 'scripts/macro_data_fetch.py') + ' freshness_check=' + (Get-BlobStatus 'scripts/freshness_check.py'))

function Invoke-RetryPython($Script, $Name, $Arguments = '', $TimeoutSeconds = 600) {
    for ($Attempt = 1; $Attempt -le 3; $Attempt++) {
        Write-Log 'START' ($Name + ' attempt ' + $Attempt)
        $Out = Join-Path $env:TEMP ([guid]::NewGuid().ToString() + '.out.log')
        $Err = Join-Path $env:TEMP ([guid]::NewGuid().ToString() + '.err.log')
        $Cmd = ('/c ""{0}" "{1}" {2} > "{3}" 2> "{4}""' -f $Python, $Script, $Arguments, $Out, $Err)
        $Psi = [System.Diagnostics.ProcessStartInfo]::new()
        $Psi.FileName = 'cmd.exe'
        $Psi.Arguments = $Cmd
        $Psi.WorkingDirectory = $Root
        $Psi.UseShellExecute = $false
        $P = [System.Diagnostics.Process]::Start($Psi)
        $TimeoutMs = [Math]::Max(1, [int]$TimeoutSeconds) * 1000
        if (-not $P.WaitForExit($TimeoutMs)) {
            $null = & taskkill.exe /PID $P.Id /T /F 2>&1
            Write-Log 'TIMEOUT' ($Name + ' exceeded ' + $TimeoutSeconds + ' seconds')
            if (Test-Path $Out) { Get-Content $Out | Add-Content $Log }
            if (Test-Path $Err) { Get-Content $Err | Add-Content $Log }
            if ($Attempt -lt 3) { Write-Log 'RETRY' ($Name + ' retrying after timeout in 60 seconds'); Start-Sleep -Seconds 60; continue }
            return $false
        }
        $P.WaitForExit()
        $ExitCode = $P.ExitCode
        if (Test-Path $Out) { Get-Content $Out | Add-Content $Log }
        if (Test-Path $Err) { Get-Content $Err | Add-Content $Log }
        if ($ExitCode -eq 0) { Write-Log 'SUCCESS' $Name; return $true }
        if ($ExitCode -eq 75) {
            # DEGRADED: source unavailable (e.g. FRED site maintenance). Not a failure -
            # retrying cannot help and the rest of the pipeline must still run.
            Write-Log 'DEGRADED' ($Name + ' reported degraded (exit 75); continuing pipeline')
            return $true
        }
        Write-Log 'ERROR' ($Name + ' exited with code ' + $ExitCode)
        if ($Attempt -lt 3) { Write-Log 'RETRY' ($Name + ' retrying after error in 60 seconds'); Start-Sleep -Seconds 60; continue }
        return $false
    }
}

$Mutex = [System.Threading.Mutex]::new($false, 'Local\DBBuilderAutoMacroDb')
$HasMutex = $false
$ExitCode = 0

try {
    try { $HasMutex = $Mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $HasMutex = $true }
    if (-not $HasMutex) {
        Write-Log 'SKIP' 'Another auto_macro_db workflow is already running'
    }
    else {
        Write-Log 'START' 'auto_macro_db workflow'
        if (-not (Invoke-RetryPython 'scripts\health_check.py' 'health_check.py')) { Write-Log 'ERROR' 'health_check.py failed after 3 retries'; $ExitCode = 1 }
        elseif (-not (Invoke-RetryPython 'scripts\security_classification_fetch.py' 'security_classification_fetch.py' '--upsert-local' 900)) { Write-Log 'ERROR' 'security_classification_fetch.py failed after 3 retries'; $ExitCode = 1 }
        elseif (-not (Invoke-RetryPython 'scripts\macro_data_fetch.py' 'macro_data_fetch.py' '' 1800)) { Write-Log 'ERROR' 'macro_data_fetch.py failed after 3 retries'; $ExitCode = 1 }
        elseif (-not (Invoke-RetryPython 'scripts\finra_short_incremental.py' 'finra_short_incremental.py' '' 1800)) { Write-Log 'ERROR' 'finra_short_incremental.py failed after 3 retries'; $ExitCode = 1 }
        elseif (-not (Invoke-RetryPython 'scripts\short_analytics_latest_sync.py' 'short_analytics_latest_sync.py' '' 1800)) { Write-Log 'ERROR' 'short_analytics_latest_sync.py failed after 3 retries'; $ExitCode = 1 }
        elseif (-not (Invoke-RetryPython 'scripts\economic_data_fetch.py' 'economic_data_fetch.py' '--upsert-local')) { Write-Log 'ERROR' 'economic_data_fetch.py failed after 3 retries'; $ExitCode = 1 }
        elseif (-not (Invoke-RetryPython 'scripts\global_economic_data_fetch.py' 'global_economic_data_fetch.py' '--upsert-local --start-date 2026-04-01' 900)) { Write-Log 'ERROR' 'global_economic_data_fetch.py failed after 3 retries'; $ExitCode = 1 }
        elseif (-not (Invoke-RetryPython 'scripts\rule_based_market_update.py' 'rule_based_market_update.py' '--save --window-hours 24 --publish-dashboard --push-dashboard --notify-telegram' 900)) { Write-Log 'ERROR' 'rule_based_market_update.py failed after 3 retries'; $ExitCode = 1 }
        else {
            Write-Log 'FRESHNESS' 'Running post-ingestion freshness validation'
            $FreshnessArgs = @('scripts\freshness_check.py','--datasets','Macro (close)','--require-critical')
            $FreshnessOutput = (& $Python @FreshnessArgs 2>&1 | Out-String)
            Add-Content -LiteralPath $Log -Value $FreshnessOutput
            $FreshnessExitCode = $LASTEXITCODE
            if ($FreshnessExitCode -eq 2) { Write-Log 'FRESHNESS FAIL' 'Critical macro data failed freshness check'; $ExitCode = 2 }
            elseif ($FreshnessExitCode -eq 3) { Write-Log 'FRESHNESS CONFIG ERROR' 'Freshness invocation/config error (usage or unknown dataset) - NOT stale data'; $ExitCode = 3 }
            elseif ($FreshnessExitCode -eq 1) { Write-Log 'SUCCESS' 'auto_macro_db workflow complete'; Write-Log 'FRESHNESS WARN' 'Non-critical freshness warnings detected' }
            else { Write-Log 'SUCCESS' 'auto_macro_db workflow complete'; Write-Log 'FRESHNESS OK' 'Macro data passed freshness check' }
        }
    }
}
finally {
    if ($HasMutex) { $Mutex.ReleaseMutex() }
    $Mutex.Dispose()
}

exit $ExitCode
