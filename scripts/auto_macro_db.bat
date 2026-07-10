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
  "$Log = Join-Path $LogDir ('auto_macro_db_' + $Stamp + '.log');" ^
  "function Write-Log($Level, $Message) { $Line = ('{0} [{1}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message); Add-Content -LiteralPath $Log -Value $Line; Write-Host $Line }" ^
  "function Invoke-RetryPython($Script, $Name, $Arguments = '', $TimeoutSeconds = 600) { for ($Attempt = 1; $Attempt -le 3; $Attempt++) { Write-Log 'START' ($Name + ' attempt ' + $Attempt); $Out = Join-Path $env:TEMP ([guid]::NewGuid().ToString() + '.out.log'); $Err = Join-Path $env:TEMP ([guid]::NewGuid().ToString() + '.err.log'); $Cmd = ('/c ""{0}"" ""{1}"" {2} > ""{3}"" 2> ""{4}""' -f $Python, $Script, $Arguments, $Out, $Err); $Psi = [System.Diagnostics.ProcessStartInfo]::new(); $Psi.FileName = 'cmd.exe'; $Psi.Arguments = $Cmd; $Psi.WorkingDirectory = $Root; $Psi.UseShellExecute = $false; $P = [System.Diagnostics.Process]::Start($Psi); $TimeoutMs = [Math]::Max(1, [int]$TimeoutSeconds) * 1000; if (-not $P.WaitForExit($TimeoutMs)) { $P.Kill(); Write-Log 'TIMEOUT' ($Name + ' exceeded ' + $TimeoutSeconds + ' seconds'); if (Test-Path $Out) { Get-Content $Out | Add-Content $Log }; if (Test-Path $Err) { Get-Content $Err | Add-Content $Log }; if ($Attempt -lt 3) { Write-Log 'RETRY' ($Name + ' retrying after timeout in 60 seconds'); Start-Sleep -Seconds 60; continue }; return $false }; $P.WaitForExit(); $ExitCode = $P.ExitCode; if (Test-Path $Out) { Get-Content $Out | Add-Content $Log }; if (Test-Path $Err) { Get-Content $Err | Add-Content $Log }; if ($ExitCode -eq 0) { Write-Log 'SUCCESS' $Name; return $true }; Write-Log 'ERROR' ($Name + ' exited with code ' + $ExitCode); if ($Attempt -lt 3) { Write-Log 'RETRY' ($Name + ' retrying after error in 60 seconds'); Start-Sleep -Seconds 60; continue }; return $false } }" ^
  "Write-Log 'START' 'auto_macro_db workflow';" ^
  "if (-not (Invoke-RetryPython 'scripts\health_check.py' 'health_check.py')) { Write-Log 'ERROR' 'health_check.py failed after 3 retries'; exit 1 }" ^
  "if (-not (Invoke-RetryPython 'scripts\security_classification_fetch.py' 'security_classification_fetch.py' '--upsert-local' 900)) { Write-Log 'ERROR' 'security_classification_fetch.py failed after 3 retries'; exit 1 }" ^
  "if (-not (Invoke-RetryPython 'scripts\macro_data_fetch.py' 'macro_data_fetch.py')) { Write-Log 'ERROR' 'macro_data_fetch.py failed after 3 retries'; exit 1 }" ^
  "if (-not (Invoke-RetryPython 'scripts\economic_data_fetch.py' 'economic_data_fetch.py' '--upsert-local')) { Write-Log 'ERROR' 'economic_data_fetch.py failed after 3 retries'; exit 1 }" ^
  "if (-not (Invoke-RetryPython 'scripts\global_economic_data_fetch.py' 'global_economic_data_fetch.py' '--upsert-local --start-date 2026-04-01' 900)) { Write-Log 'ERROR' 'global_economic_data_fetch.py failed after 3 retries'; exit 1 }" ^
  "if (-not (Invoke-RetryPython 'scripts\deepseek_multistage_report.py' 'qwen_multistage_report.py' '--save --window-hours 24 --timeout 0 --stage-timeout 0 --allow-warnings' 3600)) { Write-Log 'ERROR' 'qwen_multistage_report.py failed after 3 retries'; exit 1 }" ^
  "Write-Log 'SUCCESS' 'auto_macro_db workflow complete'; exit 0"

exit /b %ERRORLEVEL%
