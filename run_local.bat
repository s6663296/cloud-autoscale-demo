@echo off
rem Start the whole system locally: dispatch-fixed :8001, dispatch-auto :8002 (8 workers = the 8-instance cap), web :8000
rem Close the three windows (or press Ctrl+C in each) to stop.
cd /d "%~dp0"

rem --- Environment: create .venv and install requirements.txt when missing or changed ---
if exist ".venv\Scripts\python.exe" goto :deps
call :find_python
if errorlevel 1 goto :fail
echo Creating virtual environment .venv with %PY% ...
%PY% -m venv .venv
if errorlevel 1 goto :fail
:deps
rem Reinstall whenever requirements.txt differs from the copy saved after the last successful install
fc /b requirements.txt ".venv\requirements.installed" >nul 2>&1
if errorlevel 1 (
    echo Installing dependencies ...
    .venv\Scripts\python -m pip install --disable-pip-version-check -r requirements.txt
    if errorlevel 1 goto :fail
    copy /y requirements.txt ".venv\requirements.installed" >nul
)

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
exit /b 0

rem Sets PY to the first Python 3.10+ found (py launcher first, then python on PATH).
:find_python
for %%P in ("py -3" "python") do (
    %%~P -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
    if not errorlevel 1 (
        set "PY=%%~P"
        exit /b 0
    )
)
echo Python 3.10 or newer was not found. Install it from https://www.python.org/downloads/ and try again.
exit /b 1

:fail
echo.
echo Setup failed. See the messages above.
pause
exit /b 1
