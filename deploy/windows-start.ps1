param([switch]$ResearchNow)
$ErrorActionPreference = 'Stop'
$taskRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$taskPython = Join-Path $taskRoot '.venv/Scripts/python.exe'
$taskEnv = Join-Path $taskRoot '.env'
if (-not (Test-Path -LiteralPath $taskPython)) { throw 'Run uv sync --frozen first.' }
if (-not (Test-Path -LiteralPath $taskEnv)) { throw 'Create and configure .env first.' }
$taskExisting = @(Get-CimInstance Win32_Process | Where-Object {
    $_.ExecutablePath -eq $taskPython -and $_.CommandLine -match '-m\s+scout\s+bot(?:\s|$)'
})
if ($taskExisting.Count -gt 0) {
    Write-Output ('Bot is already running. Launcher PID: ' + $taskExisting[0].ProcessId)
    exit 0
}
$taskLogs = Join-Path $taskRoot 'data/logs'
New-Item -ItemType Directory -Path $taskLogs -Force | Out-Null
$taskArgs = @('-m', 'scout', 'bot', '--env-file', ('"' + $taskEnv + '"'))
if ($ResearchNow) { $taskArgs += '--research-now' }
$taskProcess = Start-Process -FilePath $taskPython -ArgumentList $taskArgs -WorkingDirectory $taskRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $taskLogs 'launcher.stdout.log') -RedirectStandardError (Join-Path $taskLogs 'launcher.stderr.log') -PassThru
Write-Output ('Bot started in background. Launcher PID: ' + $taskProcess.Id + '. Check /status in Telegram. Log: data/logs/scout.log')
