param([switch]$InspectOnly)

$ErrorActionPreference = 'Stop'
$freeProject = Split-Path -Parent $PSScriptRoot
$freePython = Join-Path $freeProject '.venv\Scripts\python.exe'
$freeSession = [System.Diagnostics.Process]::GetCurrentProcess().SessionId

# Match the desktop app, excluding renderer/helper processes and the Codex CLI.
# Do not store process command lines or inspect other users' sessions.
$freeApps = @(Get-CimInstance Win32_Process -Filter "Name='ChatGPT.exe' OR Name='Codex.exe'" |
    Where-Object {
        $_.SessionId -eq $freeSession -and
        $_.ExecutablePath -match '\\OpenAI\.Codex_[^\\]+\\app\\(ChatGPT|Codex)\.exe$' -and
        $_.CommandLine -notmatch '(?i)(--type(?:=|\s)|\s(?:app-server|exec-server)(?:\s|$))'
    } | Sort-Object CreationDate)
if ($freeApps.Count -eq 0) { exit 0 }
$freeApp = $freeApps[0]
$freeStarted = $freeApp.CreationDate.ToUniversalTime().ToString('o')
if ($InspectOnly) {
    [pscustomobject]@{ ProcessId = $freeApp.ProcessId; StartedAt = $freeStarted } |
        ConvertTo-Json -Compress
    exit 0
}
if (-not (Test-Path -LiteralPath $freePython -PathType Leaf)) { throw 'Project Python missing' }
$env:PYTHONIOENCODING = 'utf-8'
Push-Location -LiteralPath $freeProject
try {
    & $freePython -m trading_system.free_market_startup --app-started-at $freeStarted --app-pid $freeApp.ProcessId
    $freeExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $freeExitCode
