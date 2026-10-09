@echo off
rem Start the whole system locally: dispatch-fixed :8001, dispatch-auto :8002 (8 workers = the 8-instance cap), web :8000
rem Close the three windows (or press Ctrl+C in each) to stop.
cd /d "%~dp0"

rem Single-process uvicorn on Windows defaults to ProactorEventLoop, which on Python 3.10 closes the
rem listening socket after one failed accept under load (the process stays up but stops listening).
rem Force SelectorEventLoop, the same loop uvicorn already uses for --workers 8.
set "LOOP=--loop asyncio:SelectorEventLoop"

set "ALLOWED_ORIGIN=http://localhost:8000,http://127.0.0.1:8000"
start "dispatch-fixed :8001" .venv\Scripts\python -m uvicorn dispatch.main:app --port 8001 %LOOP%
start "dispatch-auto :8002" .venv\Scripts\python -m uvicorn dispatch.main:app --port 8002 --workers 8

set "FIXED_URL=http://localhost:8001"
set "AUTO_URL=http://localhost:8002"
start "web :8000" .venv\Scripts\python -m uvicorn web.main:app --port 8000 %LOOP%

timeout /t 4 /nobreak >nul
start "" http://localhost:8000
