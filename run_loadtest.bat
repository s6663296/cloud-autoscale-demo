@echo off
rem Interactive load test: choose local or cloud, then load test or capacity probe.
rem Start the services first (run_local.bat for local). Press Ctrl+C to stop early.
cd /d "%~dp0"
.venv\Scripts\python -m loadtest
echo.
pause
