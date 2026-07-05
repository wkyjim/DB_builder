@echo off
call "C:\Users\User\anaconda3\Scripts\activate.bat"
call conda activate PostgreSQL_db
cd /d "C:\Users\User\OneDrive\Coding\DB_builder"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference = 'Stop';" ^
  "$Root = 'C:\Users\User\OneDrive\Coding\DB_builder';" ^
  "$Python = 'C:\Users\User\anaconda3\envs\PostgreSQL_db\python.exe';" ^
  "$LogDir = Join-Path $Root 'logs';" ^
  "New-Item -ItemType Directory -Force -Path $LogDir | Out-Null;" ^
  "$Stamp = Get-Date -Format 'yyyyMMdd_HHmmss';" ^
  "$Log = Join-Path $LogDir ('auto_market_pulse_3h_' + $Stamp + '.log');" ^
  "function Write-Log($Level, $Message) { $Line = ('{0} [{1}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message); Add-Content -LiteralPath $Log -Value $Line; Write-Host $Line }" ^
  "function Quote-Arg($Value) { $Text = [string]$Value; if ($Text -match '[\s""]') { return '""' + ($Text -replace '""', '""""') + '""' }; return $Text }" ^
  "function Invoke-RetryPython($Script, $Name, [string[]]$ScriptArgs = @()) { for ($Attempt = 1; $Attempt -le 3; $Attempt++) { Write-Log 'START' ($Name + ' attempt ' + $Attempt); $AllArgs = @($Script) + $ScriptArgs; $ArgText = (($AllArgs | ForEach-Object { Quote-Arg $_ }) -join ' '); $Psi = [System.Diagnostics.ProcessStartInfo]::new(); $Psi.FileName = $Python; $Psi.Arguments = $ArgText; $Psi.WorkingDirectory = $Root; $Psi.UseShellExecute = $false; $Psi.RedirectStandardOutput = $true; $Psi.RedirectStandardError = $true; $P = [System.Diagnostics.Process]::new(); $P.StartInfo = $Psi; [void]$P.Start(); $OutTask = $P.StandardOutput.ReadToEndAsync(); $ErrTask = $P.StandardError.ReadToEndAsync(); if (-not $P.WaitForExit(600000)) { $P.Kill(); $P.WaitForExit(); Write-Log 'TIMEOUT' ($Name + ' exceeded 600 seconds'); Add-Content -LiteralPath $Log -Value $OutTask.Result; Add-Content -LiteralPath $Log -Value $ErrTask.Result; if ($Attempt -lt 3) { Write-Log 'RETRY' ($Name + ' retrying after timeout in 60 seconds'); Start-Sleep -Seconds 60; continue }; return $false }; $P.WaitForExit(); Add-Content -LiteralPath $Log -Value $OutTask.Result; Add-Content -LiteralPath $Log -Value $ErrTask.Result; $ExitCode = $P.ExitCode; if ($ExitCode -eq 0) { Write-Log 'SUCCESS' $Name; return $true }; Write-Log 'ERROR' ($Name + ' exited with code ' + $ExitCode); if ($Attempt -lt 3) { Write-Log 'RETRY' ($Name + ' retrying after error in 60 seconds'); Start-Sleep -Seconds 60; continue }; return $false } }" ^
  "Write-Log 'START' 'auto_market_pulse_3h workflow';" ^
  "if (-not (Invoke-RetryPython 'scripts\news_fetch.py' 'news_fetch.py' @('--upsert-local', '--limit', '200'))) { Write-Log 'ERROR' 'news_fetch.py failed after 3 retries'; exit 1 }" ^
  "if (-not (Invoke-RetryPython 'scripts\news_classify.py' 'news_classify.py' @('--upsert-local', '--limit', '100', '--max-seconds', '540'))) { Write-Log 'ERROR' 'news_classify.py failed after 3 retries'; exit 1 }" ^
  "if (-not (Invoke-RetryPython 'scripts\news_signals.py' 'news_signals.py' @('--upsert-local', '--all-windows'))) { Write-Log 'ERROR' 'news_signals.py failed after 3 retries'; exit 1 }" ^
  "if (-not (Invoke-RetryPython 'scripts\market_regime_v2.py' 'market_regime_v2.py' @('--upsert-local', '--window-hours', '3'))) { Write-Log 'ERROR' 'market_regime_v2.py failed after 3 retries'; exit 1 }" ^
  "if (-not (Invoke-RetryPython 'scripts\sector_regime.py' 'sector_regime.py' @('--upsert-local', '--window-hours', '3'))) { Write-Log 'ERROR' 'sector_regime.py failed after 3 retries'; exit 1 }" ^
  "if (-not (Invoke-RetryPython 'scripts\sector_rotation.py' 'sector_rotation.py' @('--upsert-local', '--window-hours', '3'))) { Write-Log 'ERROR' 'sector_rotation.py failed after 3 retries'; exit 1 }" ^
  "if (-not (Invoke-RetryPython 'scripts\secular_themes.py' 'secular_themes.py' @('--upsert-local', '--window-hours', '24'))) { Write-Log 'ERROR' 'secular_themes.py failed after 3 retries'; exit 1 }" ^
  "if (-not (Invoke-RetryPython 'scripts\market_pulse_report.py' 'market_pulse_report.py' @('--save', '--window-hours', '3', '--with-qwen-overlay'))) { Write-Log 'ERROR' 'market_pulse_report.py failed after 3 retries'; exit 1 }" ^
  "Write-Log 'SUCCESS' 'auto_market_pulse_3h workflow complete'; exit 0"

exit /b %ERRORLEVEL%
