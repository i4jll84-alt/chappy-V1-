# Read an existing user environment variable without displaying its value.
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
& .\.venv\Scripts\python.exe -m trading_system.cli fetch-prices --code 86970 --from 2026-10-01 --to 2026-10-22 @args
exit $LASTEXITCODE
