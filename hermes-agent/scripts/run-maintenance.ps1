param(
    [ValidateSet("quick", "daily", "weekly")]
    [string]$Job = "quick"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$AgentRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ProjectRoot = (Resolve-Path (Join-Path $AgentRoot "..")).Path
$Hermes = Join-Path $env:LOCALAPPDATA "hermes\hermes-agent\venv\Scripts\hermes.exe"
$LogDir = Join-Path $AgentRoot "logs"
$RunId = "{0}_{1}" -f $Job, (Get-Date -Format "yyyyMMdd_HHmmss")
$LogPath = Join-Path $LogDir "$RunId.log"
$LatestPointer = Join-Path $LogDir "latest-run.txt"

if (-not (Test-Path $Hermes)) {
    throw "Hermes executable not found at the expected local installation path."
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Set-Content -LiteralPath $LatestPointer -Value $LogPath -Encoding UTF8

$Prompts = @{
    quick = @"
Load the db-builder-maintenance skill. Operate at LEVEL 1 report-only.
Work through these phases in order and announce each phase before using tools:
1. Repository boundaries and Git state.
2. Material changes since hermes-agent/reports/state.md.
3. Obvious syntax, import, test, log, configuration, and structure problems.
4. Database checks only when dedicated read-only DSNs are configured.
5. Report and state updates.
Do not modify application files or databases. Update only hermes-agent/reports/latest-maintenance-report.md, project-structure.md when materially changed, and state.md. Never include secrets. Finish with a concise list of files inspected, checks run, findings, and report files updated.
"@
    daily = @"
Load the db-builder-maintenance skill. Operate at LEVEL 1 report-only.
Announce progress before each phase: Git and project health; code and configuration; safe existing tests and logs; dependencies and cross-project contracts; read-only local and Neon database health when dedicated DSNs exist; report updates. Write evidence-based P0-P3 findings to hermes-agent/reports/latest-maintenance-report.md, database-health.md, improvement-backlog.md, and state.md. No application edits, database writes, installs, deployments, commits, or pushes. Never include secrets.
"@
    weekly = @"
Load the db-builder-maintenance skill. Operate at LEVEL 1 report-only.
Announce progress before each phase. Review architecture, project boundaries, PostgreSQL and Neon design, pipelines, APIs, code quality, reliability, security, observability, dependencies, technical debt, duplication, scalability, performance, resource use, simplification, and consolidation opportunities. Update only hermes-agent/reports/weekly-optimization-report.md, improvement-backlog.md, project-structure.md, database-health.md, and state.md. Never make source or database changes and never include secrets.
"@
}

$ReportFiles = @(
    "latest-maintenance-report.md",
    "database-health.md",
    "project-structure.md",
    "improvement-backlog.md",
    "weekly-optimization-report.md",
    "state.md"
) | ForEach-Object { Join-Path (Join-Path $AgentRoot "reports") $_ }

$Before = @{}
foreach ($Path in $ReportFiles) {
    if (Test-Path $Path) {
        $Before[$Path] = (Get-Item $Path).LastWriteTimeUtc
    }
}

# A long local CPU inference must not be mistaken for a hung run.
$env:HERMES_API_TIMEOUT = "86400"
$env:PYTHONUNBUFFERED = "1"

function Write-RunLine {
    param([string]$Text)
    $Line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss K"), $Text
    Write-Host $Line
    Add-Content -LiteralPath $LogPath -Value $Line -Encoding UTF8
}

Write-RunLine "START run_id=$RunId job=$Job"
Write-RunLine "Project root: $ProjectRoot"
Write-RunLine "Model: qwen3:4b-hermes; provider: custom; no wall-clock timeout"
Write-RunLine "Hermes phase and tool previews follow. Long model-prefill periods can be quiet."

$Started = Get-Date
$Arguments = @(
    "chat", "-q", $Prompts[$Job],
    "--in", $ProjectRoot,
    "--model", "qwen3:4b-hermes",
    "--provider", "custom",
    "--reasoning", "none",
    "--skills", "db-builder-maintenance",
    "--toolsets", "terminal,file,code_execution,skills,todo",
    "--max-turns", "100",
    "--source", "db-builder-maintenance"
)

# Windows PowerShell 5 wraps native stderr as ErrorRecord objects. Hermes uses
# stderr for normal progress and warnings, so merge and log it without turning
# those records into terminating PowerShell exceptions.
$HermesJob = Start-Job -ScriptBlock {
    param($Executable, $ArgumentList, $WorkingDirectory)

    Set-Location -LiteralPath $WorkingDirectory
    $ErrorActionPreference = "Continue"
    & $Executable @ArgumentList 2>&1 | ForEach-Object { $_.ToString() }
    Write-Output ("__HERMES_EXIT_CODE__={0}" -f $LASTEXITCODE)
} -ArgumentList $Hermes, $Arguments, $ProjectRoot

$ExitCode = $null
$LastHeartbeat = Get-Date
try {
    while ($HermesJob.State -in @("NotStarted", "Running")) {
        foreach ($Item in @(Receive-Job -Job $HermesJob)) {
            $Text = $Item.ToString()
            if ($Text -match '^__HERMES_EXIT_CODE__=(-?\d+)$') {
                $ExitCode = [int]$Matches[1]
                continue
            }
            Write-Host $Text
            Add-Content -LiteralPath $LogPath -Value $Text -Encoding UTF8
        }

        if (((Get-Date) - $LastHeartbeat).TotalSeconds -ge 30) {
            $RunningFor = (Get-Date) - $Started
            Write-RunLine ("HEARTBEAT agent still running; elapsed={0:hh\:mm\:ss}; waiting for the next model or tool update" -f $RunningFor)
            $LastHeartbeat = Get-Date
        }
        Start-Sleep -Seconds 1
    }

    Wait-Job -Job $HermesJob | Out-Null
    foreach ($Item in @(Receive-Job -Job $HermesJob)) {
        $Text = $Item.ToString()
        if ($Text -match '^__HERMES_EXIT_CODE__=(-?\d+)$') {
            $ExitCode = [int]$Matches[1]
            continue
        }
        Write-Host $Text
        Add-Content -LiteralPath $LogPath -Value $Text -Encoding UTF8
    }
} finally {
    Remove-Job -Job $HermesJob -Force -ErrorAction SilentlyContinue
}

if ($null -eq $ExitCode) {
    $ExitCode = 1
    Write-RunLine "Hermes process ended without returning an exit code."
}

$Elapsed = (Get-Date) - $Started
Write-RunLine ("Hermes exit code: {0}; elapsed: {1:hh\:mm\:ss}" -f $ExitCode, $Elapsed)

$Changed = @()
foreach ($Path in $ReportFiles) {
    if (Test-Path $Path) {
        $Current = (Get-Item $Path).LastWriteTimeUtc
        if (-not $Before.ContainsKey($Path) -or $Current -gt $Before[$Path]) {
            $Changed += (Split-Path $Path -Leaf)
        }
    }
}

if ($Changed.Count -gt 0) {
    Write-RunLine ("Reports updated: " + ($Changed -join ", "))
} else {
    Write-RunLine "Reports updated: none"
}
Write-RunLine "END run_id=$RunId"
Write-Host ""
Write-Host "Permanent run log: $LogPath"

exit $ExitCode
