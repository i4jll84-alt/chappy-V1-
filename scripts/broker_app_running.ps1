# Check only this user's session. Let Windows open the configured EXE/LNK separately.
param([Parameter(Mandatory=$true)][string]$AppPath,
      [ValidateSet('rakuten','mufg_esmart')][string]$Broker)
$ErrorActionPreference = 'Stop'
$target = $AppPath
if ([IO.Path]::GetExtension($target) -ieq '.lnk') {
    $target = (New-Object -ComObject WScript.Shell).CreateShortcut($target).TargetPath
}
$vendor = if ($Broker -eq 'rakuten') {'MarketSpeed2'} else {'KabuS'}
$session = (Get-Process -Id $PID).SessionId
$shellHosts = @('rundll32.exe','dfsvc.exe','dfshim.exe','explorer.exe')
$isExe = $target -and [IO.Path]::GetExtension($target) -ieq '.exe' -and
    [IO.Path]::GetFileName($target) -notin $shellHosts
$name = if ($isExe) {[IO.Path]::GetFileNameWithoutExtension($target)} else {$vendor}
foreach ($proc in (Get-Process -Name ([WildcardPattern]::Escape($name)) -ErrorAction SilentlyContinue)) {
    if ($proc.SessionId -ne $session) {continue}
    if (-not $isExe -or [string]::Equals($proc.Path,$target,[StringComparison]::OrdinalIgnoreCase)) {
        exit 0
    }
}
exit 3
