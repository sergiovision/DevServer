#!/bin/bash
# DevServer — stop processes for the given mode (and any orphans).
#
# Usage:
#   ./scripts/stop.sh             # dev (default): stop host processes + sweep orphans
#   ./scripts/stop.sh --dev        # same
#   ./scripts/stop.sh --prod       # same (dev/prod share ports 3000/8000)
#   ./scripts/stop.sh --docker     # docker compose down
#
# Host modes (--dev/--prod) ALWAYS:
#   • SIGTERM tracked pids
#   • Free ports 3000 and 8000
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
    ;;
  dev|prod)
    stop_host || exit 1
    ;;
esac
