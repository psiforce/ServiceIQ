#!/bin/sh
# Start one ServiceIQ role. Cloud Run (and docker compose) provide $PORT.
set -e
role="${1:-api}"
[ $# -gt 0 ] && shift

case "$role" in
  api)
    exec uvicorn serviceiq.api.main:app --host 0.0.0.0 --port "$PORT" --proxy-headers --forwarded-allow-ips "*"
    ;;
  fsp)
    exec uvicorn fieldservice.main:app --host 0.0.0.0 --port "$PORT" --proxy-headers --forwarded-allow-ips "*"
    ;;
  fsp-seed)
    exec python -m fieldservice seed "$@"
    ;;
  sweep)  # one pass: Cloud Run job, triggered hourly by Cloud Scheduler
    exec python -m serviceiq sweep-approvals "$@"
    ;;
  sweep-loop)  # local compose: a pass every SWEEP_INTERVAL_SECONDS (default one hour)
    while true; do
      python -m serviceiq sweep-approvals || echo "sweep failed; retrying next interval"
      sleep "${SWEEP_INTERVAL_SECONDS:-3600}"
    done
    ;;
  chat)
    exec streamlit run apps/chat.py --server.port "$PORT" --server.address 0.0.0.0
    ;;
  admin)
    exec streamlit run apps/admin.py --server.port "$PORT" --server.address 0.0.0.0
    ;;
  *)
    # any CLI command, e.g. init-db, ingest, purge, evaluate --split heldout
    exec python -m serviceiq "$role" "$@"
    ;;
esac
