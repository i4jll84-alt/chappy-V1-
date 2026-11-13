$ErrorActionPreference='Stop'
$project=Split-Path -Parent $PSScriptRoot
$python=Join-Path $project '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Project pythonw missing' }
if ((Get-TimeZone).Id -ne 'Tokyo Standard Time') {throw 'Scheduled capture requires Asia/Tokyo'}
$name='JPX-PairedIntraday-Capture'
$argsText='-B -X utf8 -m trading_system.research.paired_start'
$existing=Get-ScheduledTask -TaskPath '\' | Where-Object TaskName -eq $name
if ($existing -and ($existing.Actions.Execute -ne $python -or $existing.Actions.Arguments -ne $argsText -or $existing.Actions.WorkingDirectory -ne $project)) { throw 'Task name conflict' }
$action=New-ScheduledTaskAction -Execute $python -Argument $argsText -WorkingDirectory $project
$trigger=New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At '08:30'
$principal=New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$settings=New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 12) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName $name -TaskPath '\' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
(Get-ScheduledTaskInfo -TaskName $name).NextRunTime.ToString('o')
