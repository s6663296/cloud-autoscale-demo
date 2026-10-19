@echo off
rem Interactive load test, old style: new customers arrive at a fixed rate (no cap on concurrent customers).
rem Start the services first (run_local.bat for local). Press Ctrl+C to stop early.
cd /d "%~dp0"
.venv\Scripts\python -m loadtest --by-rate
echo.
pause
