@echo off
call "C:\Users\User\anaconda3\Scripts\activate.bat"
call conda activate PostgreSQL_db
for %%I in ("%~dp0..") do set "DB_BUILDER_ROOT=%%~fI"
cd /d "%DB_BUILDER_ROOT%"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference = 'Stop';" ^
  "$Root = $env:DB_BUILDER_ROOT;" ^
  "$Python = 'C:\Users\User\anaconda3\envs\PostgreSQL_db\python.exe';" ^
  "$LogDir = Join-Path $Root 'logs';" ^
  "New-Item -ItemType Directory -Force -Path $LogDir | Out-Null;" ^
  "$Stamp = Get-Date -Format 'yyyyMMdd_HHmmss';" ^
  "$Log = Join-Path $LogDir ('auto_postgreSQL_db_' + $Stamp + '.log');" ^
  "function Write-Log($Level, $Message) { $Line = ('{0} [{1}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message); Add-Content -LiteralPath $Log -Value $Line; Write-Host $Line }" ^
  "function Invoke-RetryPython($Script, $Name, [string[]]$ScriptArgs = @(), [int]$TimeoutSeconds = 600) { for ($Attempt = 1; $Attempt -le 3; $Attempt++) { Write-Log 'START' ($Name + ' attempt ' + $Attempt); $ArgText = ((@($Script) + $ScriptArgs) -join ' '); $Psi = [System.Diagnostics.ProcessStartInfo]::new(); $Psi.FileName = $Python; $Psi.Arguments = $ArgText; $Psi.WorkingDirectory = $Root; $Psi.UseShellExecute = $false; $Psi.RedirectStandardOutput = $true; $Psi.RedirectStandardError = $true; $P = [System.Diagnostics.Process]::new(); $P.StartInfo = $Psi; [void]$P.Start(); $OutTask = $P.StandardOutput.ReadToEndAsync(); $ErrTask = $P.StandardError.ReadToEndAsync(); if (-not $P.WaitForExit($TimeoutSeconds * 1000)) { $P.Kill(); $P.WaitForExit(); Write-Log 'TIMEOUT' ($Name + ' exceeded ' + $TimeoutSeconds + ' seconds'); Add-Content -LiteralPath $Log -Value $OutTask.Result; Add-Content -LiteralPath $Log -Value $ErrTask.Result; if ($Attempt -lt 3) { Write-Log 'RETRY' ($Name + ' retrying after timeout in 60 seconds'); Start-Sleep -Seconds 60; continue }; return $false }; $P.WaitForExit(); Add-Content -LiteralPath $Log -Value $OutTask.Result; Add-Content -LiteralPath $Log -Value $ErrTask.Result; $ExitCode = $P.ExitCode; if ($ExitCode -eq 0) { Write-Log 'SUCCESS' $Name; return $true }; Write-Log 'ERROR' ($Name + ' exited with code ' + $ExitCode); if ($Attempt -lt 3) { Write-Log 'RETRY' ($Name + ' retrying after error in 60 seconds'); Start-Sleep -Seconds 60; continue }; return $false } }" ^
  "$Mutex = [System.Threading.Mutex]::new($false, 'Local\\DBBuilderAutoPostgreSqlDb');" ^
  "$HasMutex = $false;" ^
  "$ExitCode = 0;" ^
  "try {" ^
    "try { $HasMutex = $Mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $HasMutex = $true };" ^
    "if (-not $HasMutex) { Write-Log 'SKIP' 'Another auto_postgreSQL_db workflow is already running'; $ExitCode = 0 }" ^
    "else {" ^
      "Write-Log 'START' 'auto_postgreSQL_db workflow';" ^
      "if (-not (Invoke-RetryPython 'scripts\\health_check.py' 'health_check.py')) { Write-Log 'ERROR' 'health_check.py failed after 3 retries'; $ExitCode = 1 }" ^
      "elseif (-not (Invoke-RetryPython 'scripts\\pgSQL_equities_auto.py' 'pgSQL_equities_auto.py' @('--fetch-only') 1200)) { Write-Log 'ERROR' 'pgSQL_equities_auto.py failed after 3 retries'; $ExitCode = 1 }" ^
      "elseif (-not (Invoke-RetryPython 'scripts\\repair_historical_equity_gaps.py' 'repair_historical_equity_gaps.py' @('--lookback-days', '60', '--minimum-coverage', '0.97', '--apply-local') 3600)) { Write-Log 'ERROR' 'repair_historical_equity_gaps.py failed after 3 retries'; $ExitCode = 1 }" ^
      "elseif (-not (Invoke-RetryPython 'scripts\\pgSQL_daily_bulk_sync_to_neon.py' 'pgSQL_daily_bulk_sync_to_neon.py' @('--tables', 'equities', '--reconciliation-days', '60', '--minimum-date', '2026-05-01') 1800)) { Write-Log 'ERROR' 'pgSQL_daily_bulk_sync_to_neon.py failed after 3 retries'; $ExitCode = 1 }" ^
      "elseif (-not (Invoke-RetryPython 'scripts\\indicator_staged_backfill.py' 'indicator_staged_backfill.py' @('--stage', 'all', '--cleanup-on-success'))) { Write-Log 'ERROR' 'indicator_staged_backfill.py failed after 3 retries'; $ExitCode = 1 }" ^
      "elseif (-not (Invoke-RetryPython 'scripts\\pgSQL_daily_bulk_sync_to_neon.py' 'indicator reconciliation to Neon' @('--tables', 'indicators', '--reconciliation-days', '60', '--minimum-date', '2026-05-01') 1800)) { Write-Log 'ERROR' 'indicator reconciliation to Neon failed after 3 retries'; $ExitCode = 1 }" ^
      "else {" ^
        "Write-Log 'FRESHNESS' 'Running post-ingestion freshness validation';" ^
        "$FreshnessExitCode = (Start-Process -FilePath $Python -ArgumentList 'scripts\\freshness_check.py','--datasets','Equities (raw),Equities (indicators)','--require-critical' -Wait -PassThru -NoNewWindow).ExitCode;" ^
        "if ($FreshnessExitCode -eq 2) { Write-Log 'FRESHNESS FAIL' 'Critical datasets failed freshness check'; $ExitCode = 2 }" ^
        "elseif ($FreshnessExitCode -eq 1) { Write-Log 'SUCCESS' 'auto_postgreSQL_db workflow complete'; Write-Log 'FRESHNESS WARN' 'Non-critical freshness warnings detected' }" ^
        "else { Write-Log 'SUCCESS' 'auto_postgreSQL_db workflow complete'; Write-Log 'FRESHNESS OK' 'All critical datasets passed freshness check' }" ^
      "}" ^
    "}" ^
  "} finally {" ^
    "if ($HasMutex) { $Mutex.ReleaseMutex() }" ^
    "$Mutex.Dispose()" ^
  "}" ^
  "exit $ExitCode"

exit /b %ERRORLEVEL%
