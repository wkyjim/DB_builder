[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$PublicIp,

    [Parameter(Mandatory)]
    [string]$ApiHost,

    [Parameter(Mandatory)]
    [string]$BotHost,

    [string]$SshUser = "ubuntu",

    [string]$PrivateKey = "C:\Users\User\OneDrive\Coding\DB_builder_env\ssh\oracle\OCI-ssh-key-2026-08-23.key",

    [string]$NeonEnvPath = "C:\Users\User\OneDrive\Coding\DB_builder_env\neon-api\.env",

    [string]$TelegramEnvPath = "C:\Users\User\OneDrive\Coding\DB_builder_env\market-intelligence-telegram-bot\.env"
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$neonApi = Join-Path $projectRoot "neon-api"
$telegramBot = Join-Path $projectRoot "market-intelligence-telegram-bot"
$target = "${SshUser}@${PublicIp}"

$requiredFiles = @(
    $PrivateKey,
    $NeonEnvPath,
    $TelegramEnvPath,
    (Join-Path $neonApi "main.py"),
    (Join-Path $neonApi "requirements.txt"),
    (Join-Path $neonApi "Dockerfile"),
    (Join-Path $neonApi "privacy_policy.html"),
    (Join-Path $telegramBot "main.py"),
    (Join-Path $telegramBot "requirements.txt"),
    (Join-Path $telegramBot "Dockerfile")
)
foreach ($path in $requiredFiles) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required file not found: $path"
    }
}

$tempDir = Join-Path ([System.IO.Path]::GetTempPath()) ("oracle-market-intelligence-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $tempDir | Out-Null
try {
    $oracleEnv = Join-Path $tempDir "oracle.env"
    @"
API_HOST=$ApiHost
BOT_HOST=$BotHost
NEON_API_CONTEXT=/opt/market-intelligence/apps/neon-api
TELEGRAM_BOT_CONTEXT=/opt/market-intelligence/apps/market-intelligence-telegram-bot
NEON_API_ENV_FILE=/opt/market-intelligence/secrets/neon-api.env
TELEGRAM_BOT_ENV_FILE=/opt/market-intelligence/secrets/telegram-bot.env
"@ | Set-Content -LiteralPath $oracleEnv -Encoding ascii

    & ssh -i $PrivateKey -o IdentitiesOnly=yes $target "mkdir -p /opt/market-intelligence/apps/neon-api /opt/market-intelligence/apps/market-intelligence-telegram-bot /opt/market-intelligence/deploy /opt/market-intelligence/secrets"
    if ($LASTEXITCODE -ne 0) { throw "Could not prepare remote directories." }

    Write-Host "Uploading deployment configuration and application source..."
    & scp -i $PrivateKey -o IdentitiesOnly=yes `
        (Join-Path $PSScriptRoot "compose.yaml") `
        (Join-Path $PSScriptRoot "Caddyfile") `
        (Join-Path $PSScriptRoot "deploy.sh") `
        (Join-Path $PSScriptRoot "status.sh") `
        $oracleEnv `
        "${target}:/opt/market-intelligence/deploy/"
    if ($LASTEXITCODE -ne 0) { throw "Deployment configuration upload failed." }

    & scp -i $PrivateKey -o IdentitiesOnly=yes `
        (Join-Path $neonApi "main.py") `
        (Join-Path $neonApi "requirements.txt") `
        (Join-Path $neonApi "Dockerfile") `
        (Join-Path $neonApi "privacy_policy.html") `
        "${target}:/opt/market-intelligence/apps/neon-api/"
    if ($LASTEXITCODE -ne 0) { throw "Neon API upload failed." }

    & scp -i $PrivateKey -o IdentitiesOnly=yes `
        (Join-Path $telegramBot "main.py") `
        (Join-Path $telegramBot "requirements.txt") `
        (Join-Path $telegramBot "Dockerfile") `
        "${target}:/opt/market-intelligence/apps/market-intelligence-telegram-bot/"
    if ($LASTEXITCODE -ne 0) { throw "Telegram bot upload failed." }

    Write-Host "Uploading secret environment files without displaying their contents..."
    & scp -i $PrivateKey -o IdentitiesOnly=yes $NeonEnvPath "${target}:/opt/market-intelligence/secrets/neon-api.env"
    if ($LASTEXITCODE -ne 0) { throw "Neon API environment upload failed." }
    & scp -i $PrivateKey -o IdentitiesOnly=yes $TelegramEnvPath "${target}:/opt/market-intelligence/secrets/telegram-bot.env"
    if ($LASTEXITCODE -ne 0) { throw "Telegram bot environment upload failed." }

    Write-Host "Building and starting the Oracle Docker stack..."
    & ssh -i $PrivateKey -o IdentitiesOnly=yes $target "chmod 700 /opt/market-intelligence/secrets && chmod 600 /opt/market-intelligence/secrets/*.env && chmod +x /opt/market-intelligence/deploy/*.sh && /opt/market-intelligence/deploy/deploy.sh"
    if ($LASTEXITCODE -ne 0) { throw "Oracle stack deployment failed. Run status.sh on the VM for diagnostics." }
}
finally {
    if (Test-Path -LiteralPath $tempDir) {
        Remove-Item -LiteralPath $tempDir -Recurse -Force
    }
}

Write-Host "Oracle deployment completed successfully."
