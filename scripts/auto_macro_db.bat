@echo off
REM === BOOTSTRAP LOGGING ===
set "BOOTSTRAP_LOG=C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder\logs\auto_macro_bootstrap.log"
for %%I in ("%~dp0..\") do set "DB_BUILDER_ROOT=%%~fI"
echo [%date% %time%] BAT_START username=%username% cwd=%cd% >> "%BOOTSTRAP_LOG%"
echo [%date% %time%] DB_BUILDER_ROOT=%DB_BUILDER_ROOT% >> "%BOOTSTRAP_LOG%"

REM === CONFIGURATION ===
set "PYTHON_EXE=C:\Users\User\anaconda3\envs\PostgreSQL_db\python.exe"
set "POWERSHELL_EXE=C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
set "PS_SCRIPT=%~dp0auto_macro_db.ps1"

echo [%date% %time%] POWERSHELL_LAUNCH ps=%POWERSHELL_EXE% script=%PS_SCRIPT% python=%PYTHON_EXE% >> "%BOOTSTRAP_LOG%"

REM === CHANGE TO PROJECT ROOT ===
cd /d "%DB_BUILDER_ROOT%"
echo [%date% %time%] CWD=%cd% >> "%BOOTSTRAP_LOG%"

REM === RUN POWERSHELL WRAPPER ===
"%POWERSHELL_EXE%" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%PS_SCRIPT%"

set "WORKFLOW_EXIT_CODE=%ERRORLEVEL%"
echo [%date% %time%] POWERSHELL_EXIT=%WORKFLOW_EXIT_CODE% >> "%BOOTSTRAP_LOG%"
if not "%WORKFLOW_EXIT_CODE%"=="0" call "%~dp0telegram_system_alert.bat" "auto_macro_db workflow failed. Check scheduled logs."
exit /b %WORKFLOW_EXIT_CODE%
