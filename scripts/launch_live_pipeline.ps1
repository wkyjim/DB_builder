# Launch live pipeline as Windows background process
# DEPRECATED: This script previously detached the pipeline with redirected pipes,
# which caused the pipeline to die when the launcher exited (broken stdout/stderr pipes).
#
# FIX: Now delegates to run_live_with_env.ps1, which runs the pipeline IN-PROCESS
# with full DB connectivity validation and proper output capture.
# This keeps the pipeline alive for the full execution duration.

$root = "C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder"
$launcher = Join-Path $root "scripts\run_live_with_env.ps1"

# Ensure the launcher exists
if (-not (Test-Path $launcher)) {
    Write-Error "Launcher not found: $launcher"
    exit 1
}

# Delegate to run_live_with_env.ps1 (in-process execution with DB validation)
& $launcher
exit $LASTEXITCODE
