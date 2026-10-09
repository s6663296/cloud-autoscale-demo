#!/usr/bin/env bash
# Interactive load test: choose local or cloud, then load test or capacity probe.
# Start the services first (run_local_formac.sh for local). Press Ctrl+C to stop early.
cd "$(dirname "$0")" || exit 1
.venv/bin/python -m loadtest