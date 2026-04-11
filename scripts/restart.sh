#!/bin/bash
# DevServer — clean mode-switch restart.
#
# Tears down EVERYTHING (host processes + docker stack + orphans) before
# starting fresh in the requested mode. This is the right command to switch
# between dev / prod / docker.
#
# Usage:
#   ./scripts/restart.sh             # dev (default)
#   ./scripts/restart.sh --dev        # dev mode (hot reload)
#   ./scripts/restart.sh --prod       # prod mode (built)
#   ./scripts/restart.sh --docker     # docker compose
#
# Cross-platform: works on Linux and macOS.
set -euo pipefail

source "$(dirname "$0")/_lib.sh"
MODE="$(parse_mode "$@")"

bold "Restarting DevServer → ${MODE}"

# 1. Tear down docker if running (silent no-op if not).
docker_down || { red "Failed to stop docker stack"; exit 1; }

# 2. Tear down all host processes + orphans.
stop_host || { red "Failed to stop host processes — see ports above"; exit 1; }

# 3. Hand off to start.sh in the requested mode.
exec "${SCRIPT_DIR}/start.sh" "--${MODE}"
