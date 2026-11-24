param([switch]$Preview)
$ErrorActionPreference = 'Stop'
$healthProject = Split-Path -Parent $PSScriptRoot
$healthPython = Join-Path $healthProject '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $healthPython -PathType Leaf)) { throw 'Project Python missing' }
if ((Get-TimeZone).Id -ne 'Tokyo Standard Time') { throw 'Asia/Tokyo required' }
$healthUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$healthPrincipal = New-ScheduledTaskPrincipal -UserId $healthUser -LogonType Interactive -RunLevel Limited
$healthDays = @('Monday','Tuesday','Wednesday','Thursday','Friday')
$healthTriggers = @(
    foreach ($healthAt in @('09:30','10:00','10:30','11:00','11:30','12:00','12:30','13:00','13:30','14:00','14:30','15:00')) {
        New-ScheduledTaskTrigger -Weekly -DaysOfWeek $healthDays -At $healthAt
    }
    New-ScheduledTaskTrigger -AtLogOn -User $healthUser
)
$healthSpecs = @(
    @{Name='JPX-IntradayHealth';Arguments='-B -X utf8 -m trading_system.intraday_health';Main=$true},
    @{Name='JPX-IntradayHealth-Desktop';Arguments='-B -X utf8 -m trading_system.intraday_health_worker --broker gui';Main=$false},
    @{Name='JPX-IntradayHealth-Rakuten';Arguments='-B -X utf8 -m trading_system.intraday_health_worker --broker panel';Main=$false},
    @{Name='JPX-IntradayHealth-Kabucom';Arguments='-B -X utf8 -m trading_system.intraday_health_worker --broker kabu_panel';Main=$false}
)
$healthTasks = @()
foreach ($healthSpec in $healthSpecs) {
    $healthAction = New-ScheduledTaskAction -Execute $healthPython -Argument $healthSpec.Arguments -WorkingDirectory $healthProject
    $healthLimit = if ($healthSpec.Main) { New-TimeSpan -Minutes 5 } else { [TimeSpan]::Zero }
    $healthSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit $healthLimit -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    $healthParams = @{
        Action=$healthAction;Principal=$healthPrincipal;Settings=$healthSettings
        Description='Local intraday checks: mandatory 09:30 and 10:30 JST; half-hour retries only while abnormal. No Codex dependency, no live orders. Exchange calendar and manual stops respected.'
    }
    if ($healthSpec.Main) { $healthParams.Trigger = $healthTriggers }
    $healthTask = New-ScheduledTask @healthParams
    $healthTasks += @{Name=$healthSpec.Name;Task=$healthTask}
    $healthExisting = Get-ScheduledTask -TaskPath '\' | Where-Object TaskName -eq $healthSpec.Name
    if ($healthExisting -and (
        $healthExisting.Actions.Execute -ne $healthPython -or
        $healthExisting.Actions.Arguments -ne $healthSpec.Arguments -or
        $healthExisting.Actions.WorkingDirectory -ne $healthProject
    )) { throw ('Task name conflict: ' + $healthSpec.Name) }
}
if ($Preview) {
    $healthTasks | ForEach-Object {
        [pscustomobject]@{
            Name=$_.Name;Execute=$_.Task.Actions.Execute;Arguments=$_.Task.Actions.Arguments
            WorkingDirectory=$_.Task.Actions.WorkingDirectory
            Triggers=@($_.Task.Triggers | ForEach-Object { @{Start=$_.StartBoundary;Type=$_.CimClass.CimClassName} })
            LogonType=$_.Task.Principal.LogonType.ToString()
            MultipleInstances=$_.Task.Settings.MultipleInstances.ToString()
            ExecutionTimeLimit=$_.Task.Settings.ExecutionTimeLimit
        }
    } | ConvertTo-Json -Depth 6
    exit 0
}
foreach ($healthEntry in $healthTasks) {
    Register-ScheduledTask -TaskName $healthEntry.Name -TaskPath '\' -InputObject $healthEntry.Task -Force | Select-Object TaskName,State
}
