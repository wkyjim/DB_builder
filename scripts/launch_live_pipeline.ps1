# Launch live pipeline as Windows background process.
# Root-cause fix for cycle 20260927_1202_HKT.
#
# Old launcher detached the pipeline with in-memory redirected pipes and exited.
# When the launcher exited over SSH, the SSH channel closed and the pipes broke,
# so Write-Host in Write-Log threw under $ErrorActionPreference = 'Stop' and the
# pipeline died within ~2 seconds.
#
# New launcher starts a dedicated powershell.exe process with file-backed
# stdout/stderr so the pipeline survives the launcher's exit.

$ErrorActionPreference = 'Stop'

$root          = 'C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder'
$scriptsDir    = Join-Path $root 'scripts'
$logDir        = Join-Path $root 'logs'
$resultPath    = Join-Path $env:TEMP 'launch_result.txt'
$entryPoint    = Join-Path $scriptsDir 'run_live_with_env.ps1'

if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
}

$cycle     = Get-Date -Format 'yyyyMMdd_HHmm_HKT'
$outLog    = Join-Path $logDir "live_${cycle}.out.log"
$errLog    = Join-Path $logDir "live_${cycle}.err.log"

if (-not (Test-Path $entryPoint)) {
    Write-Error "Entry point not found: $entryPoint"
    exit 1
}

$proc = Start-Process `
    -FilePath 'powershell.exe' `
    -ArgumentList @(
        '-NoProfile',
        '-ExecutionPolicy', 'Bypass',
        '-File', $entryPoint
    ) `
    -WorkingDirectory $root `
    -RedirectStandardOutput $outLog `
    -RedirectStandardError $errLog `
    -WindowStyle Hidden `
    -PassThru

$pid        = $proc.Id
$timestamp  = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'

$lines = @(
    "Cycle=$cycle",
    "PID=$pid",
    "Status=STARTED PID=$pid",
    "StdOutLog=$outLog",
    "StdErrLog=$errLog",
    "EntryPoint=$entryPoint",
    "PipelineScript=$root\scripts\hourly_full_pipeline.ps1",
    "LauncherScript=$PSCommandPath",
    "Timestamp=$timestamp"
)
[System.IO.File]::WriteAllLines($resultPath, $lines, [System.Text.Encoding]::UTF8)

Write-Output "Cycle=$cycle"
Write-Output "PID=$pid"
Write-Output "Status=STARTED"
Write-Output "StdOutLog=$outLog"
Write-Output "StdErrLog=$errLog"
Write-Output "EntryPoint=$entryPoint"
