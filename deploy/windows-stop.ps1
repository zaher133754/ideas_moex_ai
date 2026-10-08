$ErrorActionPreference = 'Stop'
$taskRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$taskPython = Join-Path $taskRoot '.venv/Scripts/python.exe'
$taskOwned = @(Get-CimInstance Win32_Process | Where-Object {
    $_.ExecutablePath -eq $taskPython -and $_.CommandLine -match '-m\s+scout\s+bot(?:\s|$)'
})
foreach ($taskParent in $taskOwned) {
    # Stop only the verified interpreter from this project's virtual environment and its children.
    & taskkill.exe /PID $taskParent.ProcessId /T /F | Out-Null
}
if ($taskOwned.Count -eq 0) { Write-Output 'No background bot process found for this project.' }
else { Write-Output 'Local bot stopped. Reports and configuration retained.' }
