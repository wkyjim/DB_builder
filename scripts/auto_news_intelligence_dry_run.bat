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
  "$Log = Join-Path $LogDir ('auto_news_intelligence_dry_run_' + $Stamp + '.log');" ^
  "function Write-Log($Level, $Message) { $Line = ('{0} [{1}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message); Add-Content -LiteralPath $Log -Value $Line; Write-Host $Line }" ^
  "function Quote-Arg($Value) { $Text = [string]$Value; if ($Text -match '[\s""]') { return '""' + ($Text -replace '""', '""""') + '""' }; return $Text }" ^
  "function Invoke-RetryPython($Script, $Name, [string[]]$ScriptArgs = @()) { for ($Attempt = 1; $Attempt -le 3; $Attempt++) { Write-Log 'START' ($Name + ' attempt ' + $Attempt); $AllArgs = @($Script) + $ScriptArgs; $ArgText = (($AllArgs | ForEach-Object { Quote-Arg $_ }) -join ' '); $Psi = [System.Diagnostics.ProcessStartInfo]::new(); $Psi.FileName = $Python; $Psi.Arguments = $ArgText; $Psi.WorkingDirectory = $Root; $Psi.UseShellExecute = $false; $Psi.RedirectStandardOutput = $true; $Psi.RedirectStandardError = $true; $P = [System.Diagnostics.Process]::new(); $P.StartInfo = $Psi; [void]$P.Start(); $OutTask = $P.StandardOutput.ReadToEndAsync(); $ErrTask = $P.StandardError.ReadToEndAsync(); if (-not $P.WaitForExit(600000)) { $P.Kill(); $P.WaitForExit(); Write-Log 'TIMEOUT' ($Name + ' exceeded 600 seconds'); Add-Content -LiteralPath $Log -Value $OutTask.Result; Add-Content -LiteralPath $Log -Value $ErrTask.Result; if ($Attempt -lt 3) { Write-Log 'RETRY' ($Name + ' retrying after timeout in 60 seconds'); Start-Sleep -Seconds 60; continue }; return $false }; $P.WaitForExit(); Add-Content -LiteralPath $Log -Value $OutTask.Result; Add-Content -LiteralPath $Log -Value $ErrTask.Result; $ExitCode = $P.ExitCode; if ($ExitCode -eq 0) { Write-Log 'SUCCESS' $Name; return $true }; Write-Log 'ERROR' ($Name + ' exited with code ' + $ExitCode); if ($Attempt -lt 3) { Write-Log 'RETRY' ($Name + ' retrying after error in 60 seconds'); Start-Sleep -Seconds 60; continue }; return $false } }" ^
  "Write-Log 'START' 'auto_news_intelligence_dry_run workflow';" ^
  "if (-not (Invoke-RetryPython 'scripts\news_fetch.py' 'news_fetch.py' @('--dry-run', '--limit', '20'))) { Write-Log 'ERROR' 'news_fetch.py failed after 3 retries'; exit 1 }" ^
  "if (-not (Invoke-RetryPython 'scripts\news_classify.py' 'news_classify.py' @('--dry-run', '--limit', '5'))) { Write-Log 'ERROR' 'news_classify.py failed after 3 retries'; exit 1 }" ^
  "if (-not (Invoke-RetryPython 'scripts\news_signals.py' 'news_signals.py' @('--dry-run', '--all-windows'))) { Write-Log 'ERROR' 'news_signals.py failed after 3 retries'; exit 1 }" ^
  "if (-not (Invoke-RetryPython 'scripts\market_regime.py' 'market_regime.py' @('--dry-run', '--window-hours', '24'))) { Write-Log 'ERROR' 'market_regime.py failed after 3 retries'; exit 1 }" ^
  "if (-not (Invoke-RetryPython 'scripts\opportunity_report.py' 'opportunity_report.py' @('--dry-run', '--window-hours', '24'))) { Write-Log 'ERROR' 'opportunity_report.py failed after 3 retries'; exit 1 }" ^
  "if (-not (Invoke-RetryPython 'scripts\investment_report.py' 'investment_report.py' @('--dry-run', '--window-hours', '24'))) { Write-Log 'ERROR' 'investment_report.py failed after 3 retries'; exit 1 }" ^
  "Write-Log 'SUCCESS' 'auto_news_intelligence_dry_run workflow complete'; exit 0"

exit /b %ERRORLEVEL%
