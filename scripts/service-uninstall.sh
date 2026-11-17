#!/bin/bash
# DevServer — remove the macOS launchd service (label: devserver.service).
#
# Usage:
#   ./scripts/service-uninstall.sh
#
# Unloads the agent and deletes its plist. Leaves the checkout, the database
# and the logs alone — this only removes the "start at login" wiring.
set -euo pipefail

source "$(dirname "$0")/_lib.sh"

LABEL="devserver.service"
PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
DOMAIN="gui/$(id -u)"

bold "Removing ${LABEL}"

if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  green "  Unloaded from ${DOMAIN}"
else
  echo "  Not loaded."
fi

if [[ -f "$PLIST" ]]; then
  rm -f "$PLIST"
  green "  Deleted ${PLIST}"
else
  echo "  No plist at ${PLIST}"
fi

# launchd sends SIGTERM on bootout and the supervisor takes its children with
# it, but a hard-killed previous run can leave the ports held.
if port_busy "$WORKER_PORT" || port_busy "$WEB_PORT"; then
  yellow "  Ports ${WORKER_PORT}/${WEB_PORT} still busy — stopping host processes..."
  stop_host >/dev/null 2>&1 || true
fi

echo
green "${LABEL} removed. DevServer will no longer start at login."
echo "  Start it by hand with: ./scripts/start.sh"
