#!/usr/bin/env bash
# Start the whole system locally: dispatch-fixed :8001, dispatch-auto :8002 (8 workers = the 8-instance cap), web :8000
# macOS / Linux counterpart of run_local.bat. Press Ctrl+C to stop all three services.

# pretesk: Terminal: 
#   chmod +x run_local_formac.sh run_loadtest_formac.sh
#   ./run_local.sh

cd "$(dirname "$0")" || exit 1

fail() {
    echo
    echo "Setup failed. See the messages above."
    exit 1
}

# Prints the first Python 3.10+ found on PATH.
find_python() {
    local p
    for p in python3 python; do
        if command -v "$p" >/dev/null 2>&1 \
            && "$p" -c "import sys; sys.exit(sys.version_info < (3, 10))" >/dev/null 2>&1; then
            echo "$p"
            return 0
        fi
    done
    return 1
}

# --- Environment: create .venv and install requirements.txt when missing or changed ---
if [ ! -x ".venv/bin/python" ]; then
    PY="$(find_python)" || {
        echo "Python 3.10 or newer was not found. Install it (e.g. brew install python@3.12) and try again."
        fail
    }
    echo "Creating virtual environment .venv with $PY ..."
    "$PY" -m venv .venv || fail
fi

# Reinstall whenever requirements.txt differs from the copy saved after the last successful install
if ! cmp -s requirements.txt .venv/requirements.installed; then
    echo "Installing dependencies ..."
    .venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt || fail
    cp requirements.txt .venv/requirements.installed
fi

PIDS=()
cleanup() {
    trap - INT TERM EXIT
    echo
    echo "Stopping services ..."
    for pid in "${PIDS[@]}"; do
        kill "$pid" 2>/dev/null
    done
    wait 2>/dev/null
    exit 0
}
trap cleanup INT TERM EXIT

# No --loop override needed: the ProactorEventLoop problem is Windows-only.
export ALLOWED_ORIGIN="http://localhost:8000,http://127.0.0.1:8000"
.venv/bin/python -m uvicorn dispatch.main:app --port 8001 &
PIDS+=($!)
.venv/bin/python -m uvicorn dispatch.main:app --port 8002 --workers 8 &
PIDS+=($!)

export FIXED_URL="http://localhost:8001"
export AUTO_URL="http://localhost:8002"
.venv/bin/python -m uvicorn web.main:app --port 8000 &
PIDS+=($!)

sleep 4
if command -v open >/dev/null 2>&1; then
    open "http://localhost:8000"
elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "http://localhost:8000" >/dev/null 2>&1
fi

echo "dispatch-fixed :8001, dispatch-auto :8002, web :8000 are running. Press Ctrl+C to stop."
wait