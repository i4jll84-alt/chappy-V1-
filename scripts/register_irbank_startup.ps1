param([switch]$Preview)

$ErrorActionPreference = 'Stop'
$irProject = Split-Path -Parent $PSScriptRoot
$irScript = Join-Path $PSScriptRoot 'check_irbank_startup.ps1'
$irPython = Join-Path $irProject '.venv\Scripts\pythonw.exe'
foreach ($irRequired in @($irScript, $irPython)) {
    if (-not (Test-Path -LiteralPath $irRequired -PathType Leaf)) { throw 'Required local executable or script missing' }
}
$irPolicy = Get-Content -LiteralPath (Join-Path $irProject 'data\irbank_jsf\acquisition-policy.json') -Raw | ConvertFrom-Json
$irExpiry = [DateTimeOffset]::Parse($irPolicy.ends_before)
if ([DateTimeOffset]::Now -ge $irExpiry) { throw 'Free trial has already ended' }
$irUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$irName = 'IRBANK-Trial-CodexStartup'
$irCodexRoot = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path ([Environment]::GetFolderPath('UserProfile')) '.codex' }
$irAutomation = Join-Path $irCodexRoot 'automations\irbank-5\automation.toml'
$irArguments = '-B -X utf8 -m trading_system.research.irbank_startup --detect-app --project "' + $irProject + '" --automation "' + $irAutomation + '"'
$irAction = New-ScheduledTaskAction -Execute $irPython -Argument $irArguments -WorkingDirectory $irProject
$irTimer = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$irTimer.EndBoundary = $irExpiry.ToString('yyyy-MM-ddTHH:mm:sszzz')
$irTimer.Repetition.StopAtDurationEnd = $false
$irLogon = New-ScheduledTaskTrigger -AtLogOn -User $irUser
$irLogon.EndBoundary = $irExpiry.ToString('yyyy-MM-ddTHH:mm:sszzz')
$irPrincipal = New-ScheduledTaskPrincipal -UserId $irUser -LogonType Interactive -RunLevel Limited
$irSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -DeleteExpiredTaskAfter (New-TimeSpan -Days 1)
$irTask = New-ScheduledTask -Action $irAction -Trigger @($irTimer, $irLogon) -Principal $irPrincipal -Settings $irSettings -Description 'Resume a missed IRBANK 21:00 JST run when Codex desktop is available; preserve daily quota and free-trial deadline.'
if ($Preview) {
    [pscustomobject]@{
        TaskName=$irName; Execute=$irAction.Execute; Arguments=$irAction.Arguments
        WorkingDirectory=$irProject; Interval=$irTimer.Repetition.Interval
        Start=$irTimer.StartBoundary; End=$irTimer.EndBoundary; LogonTrigger=$true
        User=$irUser; LogonType=$irPrincipal.LogonType.ToString(); RunLevel=$irPrincipal.RunLevel.ToString()
        MultipleInstances=$irSettings.MultipleInstances.ToString()
        StartWhenAvailable=$irSettings.StartWhenAvailable; WakeToRun=$irSettings.WakeToRun
        DeleteExpiredTaskAfter=$irSettings.DeleteExpiredTaskAfter
    } | ConvertTo-Json
    exit 0
}
$irExisting = Get-ScheduledTask -TaskPath '\' -ErrorAction Stop | Where-Object TaskName -eq $irName
if ($irExisting -and (($irExisting.Actions.Arguments -ne $irArguments) -or ($irExisting.Actions.Execute -ne $irPython))) {
    throw 'Task name is already used by a different action'
}
Register-ScheduledTask -TaskName $irName -TaskPath '\' -InputObject $irTask -Force | Select-Object TaskName,State
