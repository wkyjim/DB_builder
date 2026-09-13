[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$PublicIp,

    [string]$SshUser = "ubuntu",

    [string]$PrivateKey = "C:\Users\User\OneDrive\Coding\DB_builder_env\ssh\oracle\OCI-ssh-key-2026-08-23.key"
)

$ErrorActionPreference = "Stop"
$scriptPath = Join-Path $PSScriptRoot "bootstrap-ubuntu.sh"

foreach ($path in @($PrivateKey, $scriptPath)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required file not found: $path"
    }
}

$target = "${SshUser}@${PublicIp}"
Write-Host "Uploading the Oracle VM bootstrap script..."
& scp -i $PrivateKey -o IdentitiesOnly=yes $scriptPath "${target}:/tmp/bootstrap-ubuntu.sh"
if ($LASTEXITCODE -ne 0) { throw "Bootstrap upload failed." }

Write-Host "Installing Docker and configuring the host firewall..."
& ssh -i $PrivateKey -o IdentitiesOnly=yes $target "sudo bash /tmp/bootstrap-ubuntu.sh"
if ($LASTEXITCODE -ne 0) { throw "Oracle VM bootstrap failed." }

Write-Host "Bootstrap completed. Reconnect before running Deploy-OracleStack.ps1."
