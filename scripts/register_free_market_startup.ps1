param([switch]$Preview)

$ErrorActionPreference = 'Stop'
$freeProject = Split-Path -Parent $PSScriptRoot
$freeScript = Join-Path $PSScriptRoot 'check_free_market_startup.ps1'
$freePython = Join-Path $freeProject '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $freePython -PathType Leaf)) { throw 'Project Python missing' }
if (-not (Test-Path -LiteralPath $freeScript -PathType Leaf)) { throw 'Startup checker missing' }
$freeUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$freeTaskName = 'JPX-FreeMarket-CodexStartup'
$freePowerShell = Join-Path $PSHOME 'pwsh.exe'
if (-not (Test-Path -LiteralPath $freePowerShell -PathType Leaf)) { throw 'Run this installer from PowerShell 7' }
$freeArguments = '-NoProfile -NonInteractive -WindowStyle Hidden -File "' + $freeScript + '"'
$freeAction = New-ScheduledTaskAction -Execute $freePowerShell -Argument $freeArguments -WorkingDirectory $freeProject
# Local process checks only. No chat/model call, no repeated acquisition.
$freeTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$freeTrigger.Repetition.StopAtDurationEnd = $false
$freePrincipal = New-ScheduledTaskPrincipal -UserId $freeUser -LogonType Interactive -RunLevel Limited
$freeSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$freeTask = New-ScheduledTask -Action $freeAction -Trigger $freeTrigger -Principal $freePrincipal -Settings $freeSettings -Description 'Free-market catch-up once, 30 minutes after Codex desktop starts. Weekday 08:00 JST is managed by Codex automation jpx.'
if ($Preview) {
    [pscustomobject]@{
        TaskName = $freeTaskName
        Execute = $freeAction.Execute
        Arguments = $freeAction.Arguments
        WorkingDirectory = $freeAction.WorkingDirectory
        Start = $freeTrigger.StartBoundary
        Interval = $freeTrigger.Repetition.Interval
        Duration = $freeTrigger.Repetition.Duration
        User = $freePrincipal.UserId
        LogonType = $freePrincipal.LogonType.ToString()
        RunLevel = $freePrincipal.RunLevel.ToString()
        MultipleInstances = $freeSettings.MultipleInstances.ToString()
        WakeToRun = $freeSettings.WakeToRun
    } | ConvertTo-Json
    exit 0
}
$freeExisting = Get-ScheduledTask -TaskPath '\' -ErrorAction Stop | Where-Object TaskName -eq $freeTaskName
if ($freeExisting -and ($freeExisting.Actions.Arguments -ne $freeArguments)) {
    throw 'Task name is already used by a different action; inspect before replacing'
}
Register-ScheduledTask -TaskName $freeTaskName -TaskPath '\' -InputObject $freeTask -Force | Select-Object TaskName,State
