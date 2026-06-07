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
  "function Invoke-RetryPython($Script, $Name) { for ($Attempt = 1; $Attempt -le 3; $Attempt++) { Write-Log 'START' ($Name + ' attempt ' + $Attempt); $Out = Join-Path $env:TEMP ([guid]::NewGuid().ToString() + '.out.log'); $Err = Join-Path $env:TEMP ([guid]::NewGuid().ToString() + '.err.log'); $Cmd = ('/c ""{0}"" ""{1}"" > ""{2}"" 2> ""{3}""' -f $Python, $Script, $Out, $Err); $Psi = [System.Diagnostics.ProcessStartInfo]::new(); $Psi.FileName = 'cmd.exe'; $Psi.Arguments = $Cmd; $Psi.WorkingDirectory = $Root; $Psi.UseShellExecute = $false; $P = [System.Diagnostics.Process]::Start($Psi); if (-not $P.WaitForExit(600000)) { $P.Kill(); Write-Log 'TIMEOUT' ($Name + ' exceeded 600 seconds'); if (Test-Path $Out) { Get-Content $Out | Add-Content $Log }; if (Test-Path $Err) { Get-Content $Err | Add-Content $Log }; if ($Attempt -lt 3) { Write-Log 'RETRY' ($Name + ' retrying after timeout in 60 seconds'); Start-Sleep -Seconds 60; continue }; return $false }; $P.WaitForExit(); $ExitCode = $P.ExitCode; if (Test-Path $Out) { Get-Content $Out | Add-Content $Log }; if (Test-Path $Err) { Get-Content $Err | Add-Content $Log }; if ($ExitCode -eq 0) { Write-Log 'SUCCESS' $Name; return $true }; Write-Log 'ERROR' ($Name + ' exited with code ' + $ExitCode); if ($Attempt -lt 3) { Write-Log 'RETRY' ($Name + ' retrying after error in 60 seconds'); Start-Sleep -Seconds 60; continue }; return $false } }" ^
  "Write-Log 'START' 'auto_macro_db workflow';" ^
  "if (-not (Invoke-RetryPython 'scripts\health_check.py' 'health_check.py')) { Write-Log 'ERROR' 'health_check.py failed after 3 retries'; exit 1 }" ^
  "if (-not (Invoke-RetryPython 'scripts\macro_data_fetch.py' 'macro_data_fetch.py')) { Write-Log 'ERROR' 'macro_data_fetch.py failed after 3 retries'; exit 1 }" ^
  "Write-Log 'SUCCESS' 'auto_macro_db workflow complete'; exit 0"

exit /b %ERRORLEVEL%
