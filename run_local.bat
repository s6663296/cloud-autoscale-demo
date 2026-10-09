@echo off
rem Start the whole system locally: dispatch-fixed :8001, dispatch-auto :8002 (4 workers), web :8000
rem Close the three windows (or press Ctrl+C in each) to stop.
cd /d "%~dp0"

set "ALLOWED_ORIGIN=http://localhost:8000,http://127.0.0.1:8000"
start "dispatch-fixed :8001" .venv\Scripts\python -m uvicorn dispatch.main:app --port 8001
start "dispatch-auto :8002" .venv\Scripts\python -m uvicorn dispatch.main:app --port 8002 --workers 4

set "FIXED_URL=http://localhost:8001"
set "AUTO_URL=http://localhost:8002"
start "web :8000" .venv\Scripts\python -m uvicorn web.main:app --port 8000

timeout /t 4 /nobreak >nul
start "" http://localhost:8000
