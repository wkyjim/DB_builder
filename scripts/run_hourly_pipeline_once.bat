@echo off
REM ===========================================================================
REM Hourly full-refresh pipeline - MANUAL RUN wrapper (Phase 1)
REM
REM Runs ONE complete cycle: parallel data refresh -> full latest-report.md
REM generation -> atomic publish -> commit/push -> state JSON.
REM
REM NOT scheduled. Run manually from the Windows host:
REM   scripts\run_hourly_pipeline_once.bat
REM   scripts\run_hourly_pipeline_once.bat --dry-run
REM
REM Exit codes: 0 = REPORT_READY (check state JSON for publication status)
REM             1 = FAILED (critical source or report failure)
REM ===========================================================================

for %%I in ("%~dp0..") do set "DB_BUILDER_ROOT=%%~fI"
cd /d "%DB_BUILDER_ROOT%"

set "PS_ARGS=-NoProfile -ExecutionPolicy Bypass -File scripts\hourly_full_pipeline.ps1"
if not "%~1"=="" set "PS_ARGS=%PS_ARGS% %*"

powershell %PS_ARGS%
exit /b %ERRORLEVEL%
