#Requires -RunAsAdministrator

$ErrorActionPreference = "Stop"

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$taskPath = "\My Programs\"
$updates = [ordered]@{
    "Fetch Macro Data Asia" = "auto_macro_db.bat"
    "News_fetcher" = "auto_news_intelligence.bat"
    "Run_US_Equities_pgSQL_Query" = "auto_postgreSQL_db.bat"
}

foreach ($entry in $updates.GetEnumerator()) {
    $scriptPath = Join-Path $PSScriptRoot $entry.Value
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
        throw "Scheduled-task launcher not found: $scriptPath"
    }

    $task = Get-ScheduledTask -TaskPath $taskPath -TaskName $entry.Key
    $action = New-ScheduledTaskAction -Execute $scriptPath -WorkingDirectory $PSScriptRoot
    Set-ScheduledTask -TaskPath $task.TaskPath -TaskName $task.TaskName -Action $action | Out-Null

    $updated = Get-ScheduledTask -TaskPath $task.TaskPath -TaskName $task.TaskName
    $updatedAction = $updated.Actions | Select-Object -First 1
    if ($updatedAction.Execute -ne $scriptPath -or $updatedAction.WorkingDirectory -ne $PSScriptRoot) {
        throw "Scheduled-task path verification failed: $($task.TaskName)"
    }

    Write-Output "Updated $($task.TaskName) -> $scriptPath"
}

Write-Output "Scheduled-task relocation complete for $root"
