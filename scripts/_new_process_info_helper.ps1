function New-ProcessInfo {
    param(
        [string]$Python,
        [string]$ScriptPath,
        [string]$WorkingDirectory,
        [string]$ExtraArgs = ''
    )
    $scriptName = Split-Path $ScriptPath -LeafBase
    $logDir = Join-Path $WorkingDirectory 'reports\state\logs'
    if (-not (Test-Path $logDir)) {
        New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    }
    $branchLog = Join-Path $logDir ($scriptName + '.log')
    $cmdArgs = '/c "' + $Python + '" -u "' + $ScriptPath + '" ' + $ExtraArgs + ' > "' + $branchLog + '" 2>&1'
    $pinfo = New-Object System.Diagnostics.ProcessStartInfo
    $pinfo.FileName = 'cmd.exe'
    $pinfo.Arguments = $cmdArgs
    $pinfo.WorkingDirectory = $WorkingDirectory
    $pinfo.UseShellExecute = $false
    $pinfo.CreateNoWindow = $true
    $pinfo.EnvironmentVariables['PYTHONUNBUFFERED'] = '1'
    $pinfo.EnvironmentVariables['PYTHONPATH'] = (Join-Path $WorkingDirectory 'src') + ';' + (Join-Path $WorkingDirectory 'scripts')
    $envVars = @('LOCAL_DATABASE_URL','LOCAL_DB_HOST','LOCAL_DB_PORT','LOCAL_DB_NAME','LOCAL_DB_USER','LOCAL_DB_PASSWORD','LOCAL_ENV_DIR','DB_BUILDER_ENV_DIR','NEON_DATABASE_URL','NEON_DB_HOST','NEON_DB_NAME','NEON_DB_USER','NEON_DB_PASSWORD')
    foreach ($var in $envVars) {
        $val = [System.Environment]::GetEnvironmentVariable($var)
        if (-not [string]::IsNullOrWhiteSpace($val)) {
            $pinfo.EnvironmentVariables[$var] = $val
        }
    }
    return $pinfo, $branchLog
}
