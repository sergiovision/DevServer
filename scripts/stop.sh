#!/bin/bash
# DevServer — stop processes for the given mode (and any orphans).
#
# Usage:
#   ./scripts/stop.sh             # dev (default): stop host processes + sweep orphans
#   ./scripts/stop.sh --dev        # same
#   ./scripts/stop.sh --prod       # same (dev/prod share the WEB_PORT/WORKER_PORT, default 3200/8000)
#   ./scripts/stop.sh --docker     # docker compose down
#
# Host modes (--dev/--prod) ALWAYS:
#   • SIGTERM tracked pids
#   • Free the WEB_PORT and WORKER_PORT (default 3200 and 8000)
#   • Sweep any process spawned from apps/web or apps/worker (orphan cleanup)
#   • Verify ports are free; escalate to SIGKILL if not
#
# Cross-platform: works on Linux and macOS (uses lsof when available, falls back to ss).
set -euo pipefail

source "$(dirname "$0")/_lib.sh"
MODE="$(parse_mode "$@")"

case "$MODE" in
  docker)
    docker_down || { red "docker compose down failed"; exit 1; }
    # In the host-worker topology the worker runs on the host, so the docker
    # teardown above won't stop it — clean up the host process too. Run after
    # docker_down so the web container has already released WEB_PORT.
    if docker_is_host_worker; then
      stop_host || exit 1
    fi
    ;;
  dev|prod)
    stop_host || exit 1
    ;;
esac
