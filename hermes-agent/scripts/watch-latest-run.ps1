Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$AgentRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Pointer = Join-Path (Join-Path $AgentRoot "logs") "latest-run.txt"

if (-not (Test-Path $Pointer)) {
    throw "No Hermes maintenance run has been started yet."
}

$LogPath = (Get-Content -LiteralPath $Pointer -Raw).Trim()
if (-not (Test-Path $LogPath)) {
    throw "The latest run log does not exist: $LogPath"
}

Write-Host "Following $LogPath"
Get-Content -LiteralPath $LogPath -Wait -Tail 40
