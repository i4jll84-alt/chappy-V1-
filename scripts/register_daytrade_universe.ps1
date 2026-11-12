param()
$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$python = Join-Path $project '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Project pythonw missing' }
if ((Get-TimeZone).Id -ne 'Tokyo Standard Time') { throw 'This schedule requires Asia/Tokyo' }
$name = 'JPX-DaytradeUniverse-AfterClose'
$arguments = '-B -X utf8 -m trading_system.research.swing_daily --latest-closed-session'
$existing = Get-ScheduledTask -TaskPath '\' | Where-Object TaskName -eq $name
if ($existing -and ($existing.Actions.Execute -ne $python -or
    $existing.Actions.Arguments -ne $arguments -or $existing.Actions.WorkingDirectory -ne $project)) {
    throw 'Task name already used by a different action'
}
$action = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $project
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At '18:00'
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 15) -ExecutionTimeLimit (New-TimeSpan -Minutes 45)
Register-ScheduledTask -TaskName $name -TaskPath '\' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Select-Object TaskName,State
Get-ScheduledTaskInfo -TaskName $name | Select-Object NextRunTime
