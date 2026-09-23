<#
.SYNOPSIS
    One-time setup: install the market-dashboard deploy key for Windows-host Git.

.DESCRIPTION
    Fixes the host-side Git authentication failure that has blocked
    latest-report.md publication since 21 Sep. The container-only path
    /var/tmp/market_dashboard_deploy is invisible to Windows Task Scheduler;
    this script installs a persistent user-scoped copy and sets the
    repo-local SSH command to use it.

    IMPORTANT - run ONCE on the Windows host from an elevated or normal
    PowerShell. It NEVER prints or logs the private key contents.

    Prerequisites (manual, one-time):
      1. The market-dashboard deploy key must exist as a file. Copy it from
         the mounted secret location to a TEMPORARY host path, e.g.:
             C:\Users\User\Downloads\market_dashboard_deploy
      2. GitHub known_hosts entry (github.com) - this script reuses the
         existing known_hosts if present, else accepts-new on first connect.

    What this script does:
      - Creates C:\Users\User\.ssh\ if needed
      - Copies the key from the temp location to
        C:\Users\User\.ssh\market_dashboard_deploy
      - Restricts ACLs to the current user only (Windows chmod-600 equivalent)
      - Sets the market-dashboard repo-local core.sshCommand (repo-local,
        does NOT affect other repos or the container's usage)
      - Runs a non-destructive verification: git ls-remote + git fetch

    Rollback:
      Remove the key file and clear the repo config:
        git -C market-dashboard config --unset core.sshCommand
        Remove-Item C:\Users\User\.ssh\market_dashboard_deploy
#>

param(
    [Parameter(Mandatory = $true)]
    [string]$TempKeyPath,
    [string]$RepoPath = 'C:\Users\User\OneDrive\Coding\Hermes_PM\DB_builder\market-dashboard'
)

$ErrorActionPreference = 'Stop'

$SshDir = Join-Path $env:USERPROFILE '.ssh'
$KeyDest = Join-Path $SshDir 'market_dashboard_deploy'

if (-not (Test-Path $TempKeyPath)) {
    Write-Error "Temporary key file not found: $TempKeyPath`nCopy the deploy key to that path first (e.g. from the mounted secret)."
    exit 1
}
if (-not (Test-Path (Join-Path $RepoPath '.git'))) {
    Write-Error "market-dashboard repo not found at: $RepoPath"
    exit 1
}

# 1. Install the key
New-Item -ItemType Directory -Force -Path $SshDir | Out-Null
Copy-Item -LiteralPath $TempKeyPath -Destination $KeyDest -Force
Write-Host "[OK] Key installed at $KeyDest (contents never displayed)"

# 2. Restrict ACL to current user only (Windows equivalent of chmod 600)
$acl = Get-Acl $KeyDest
$acl.SetAccessRuleProtection($true, $false)
$rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
    $env:USERNAME, 'FullControl', 'Allow')
$acl.SetAccessRule($rule)
Set-Acl -LiteralPath $KeyDest $acl
Write-Host "[OK] ACL restricted to $env:USERNAME only"

# 3. Set repo-local SSH command (does not touch global config)
$sshCmd = "ssh -i `"$($KeyDest -replace '\\','/')`" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
& git -C $RepoPath config core.sshCommand $sshCmd
if ($LASTEXITCODE -ne 0) { Write-Error 'Failed to set core.sshCommand'; exit 1 }
Write-Host "[OK] Repo-local core.sshCommand set (repo-scoped, container unaffected)"

# 4. Non-destructive verification: ls-remote then fetch
Write-Host ''
Write-Host '=== Verification: git ls-remote (read-only) ==='
& git -C $RepoPath ls-remote origin main
if ($LASTEXITCODE -ne 0) {
    Write-Error 'VERIFICATION FAILED: git ls-remote could not authenticate. Check the key is the correct market-dashboard deploy key.'
    exit 1
}
Write-Host '[OK] ls-remote authenticated'

Write-Host '=== Verification: git fetch origin main (read-only) ==='
& git -C $RepoPath fetch origin main
if ($LASTEXITCODE -ne 0) {
    Write-Error 'VERIFICATION FAILED: git fetch failed.'
    exit 1
}
Write-Host '[OK] fetch authenticated'

Write-Host ''
Write-Host 'SUCCESS: Windows-host Git authentication for market-dashboard is fixed.'
Write-Host 'The hourly pipeline (hourly_full_pipeline.ps1) can now publish.'
Write-Host 'You may delete the temporary key copy at:' $TempKeyPath
