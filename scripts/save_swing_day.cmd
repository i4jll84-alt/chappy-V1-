@echo off
setlocal
pushd "%~dp0.." || exit /b 1
".venv\Scripts\python.exe" -B -X utf8 -m trading_system.research.swing_daily %*
set "swing_result=%ERRORLEVEL%"
popd
pause
exit /b %swing_result%
