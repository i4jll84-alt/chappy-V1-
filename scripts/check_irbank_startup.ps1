param([switch]$InspectOnly)
$ErrorActionPreference = 'Stop'
$irProject = Split-Path -Parent $PSScriptRoot
$irPython = Join-Path $irProject '.venv\Scripts\python.exe'
$irCodexRoot = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path ([Environment]::GetFolderPath('UserProfile')) '.codex' }
$irAutomation = Join-Path $irCodexRoot 'automations\irbank-5\automation.toml'
$irArguments = @('-B', '-X', 'utf8', '-m', 'trading_system.research.irbank_startup',
 '--project', $irProject, '--automation', $irAutomation, '--detect-app')
if ($InspectOnly) { $irArguments += '--inspect' }
Push-Location -LiteralPath $irProject
try {
 & $irPython @irArguments
 $irExitCode = $LASTEXITCODE
} finally { Pop-Location }
exit $irExitCode
