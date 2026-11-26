param([switch]$Preview)
$ErrorActionPreference = 'Stop'
$reportProject = Split-Path -Parent $PSScriptRoot
$reportPython = Join-Path $reportProject '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $reportPython -PathType Leaf)) { throw 'Project pythonw missing' }
if ((Get-TimeZone).Id -ne 'Tokyo Standard Time') { throw 'This schedule requires Asia/Tokyo' }
$reportName = 'JPX-ForwardReports-AfterClose'
$reportArguments = '-B -X utf8 -m trading_system.research.forward_reports --scheduled'
$reportExisting = Get-ScheduledTask -TaskPath '\' | Where-Object TaskName -eq $reportName
if ($reportExisting -and ($reportExisting.Actions.Execute -ne $reportPython -or
    $reportExisting.Actions.Arguments -ne $reportArguments -or $reportExisting.Actions.WorkingDirectory -ne $reportProject)) {
    throw 'Task name already used by a different action'
}
$reportUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$reportAction = New-ScheduledTaskAction -Execute $reportPython -Argument $reportArguments -WorkingDirectory $reportProject
$reportDaily = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At '22:50'
$reportLogon = New-ScheduledTaskTrigger -AtLogOn -User $reportUser
$reportLogon.Delay = 'PT5M'
$reportPrincipal = New-ScheduledTaskPrincipal -UserId $reportUser -LogonType Interactive -RunLevel Limited
$reportSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 15) -ExecutionTimeLimit (New-TimeSpan -Minutes 15)
$reportTask = New-ScheduledTask -Action $reportAction -Trigger @($reportDaily, $reportLogon) -Principal $reportPrincipal -Settings $reportSettings -Description 'Read-only daily, weekly and yearly forward paper PDFs. Checks the linked Codex automation ACTIVE gate; no trading or data retrieval.'
if ($Preview) {
    [pscustomobject]@{
        TaskName=$reportName; Execute=$reportPython; Arguments=$reportArguments
        WorkingDirectory=$reportProject; Daily=$reportDaily.StartBoundary
        LogonDelay=$reportLogon.Delay; StartWhenAvailable=$reportSettings.StartWhenAvailable
        RestartCount=$reportSettings.RestartCount; RestartInterval=$reportSettings.RestartInterval
        LogonType=$reportPrincipal.LogonType.ToString(); RunLevel=$reportPrincipal.RunLevel.ToString()
    } | ConvertTo-Json
    exit 0
}
Register-ScheduledTask -TaskName $reportName -TaskPath '\' -InputObject $reportTask -Force | Select-Object TaskName,State
Get-ScheduledTaskInfo -TaskName $reportName | Select-Object NextRunTime
