#!/bin/bash
# DevServer — install the macOS launchd service (label: devserver.service).
#
# Usage:
#   ./scripts/service-install.sh            # prod mode (default)
#   ./scripts/service-install.sh --dev      # dev mode (hot reload)
#   ./scripts/service-install.sh --no-build # skip the pre-build
#
# Installs a **LaunchAgent**, not a LaunchDaemon, deliberately:
#
#   - The worker spawns `claude` / `codex`, which authenticate against OAuth
#     logins stored in ~/.claude and ~/.codex. A root daemon has neither.
#   - The session observatory reads ~/.claude and ~/.codex.
#   - Local-provider repos live under the operator's home.
#   - PostgreSQL on this machine is itself a LaunchAgent
#     (homebrew.mxcl.postgresql@NN), so a boot-time daemon would start before
#     the database exists at all.
#
# A LaunchAgent starts at **login** rather than at boot. On a single-user Mac
# that is the same moment in practice, and it is the only placement where
# DevServer has the credentials it needs.
set -euo pipefail

source "$(dirname "$0")/_lib.sh"

LABEL="devserver.service"
PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
DOMAIN="gui/$(id -u)"

MODE="prod"
DO_BUILD=1
for arg in "$@"; do
  case "$arg" in
    --dev)      MODE="dev" ;;
    --prod)     MODE="prod" ;;
    --no-build) DO_BUILD=0 ;;
  esac
done

if [[ "$IS_MACOS" != "1" ]]; then
  red "This installer is macOS-only (launchd). On Linux use a systemd unit."
  exit 1
fi

bold "Installing ${LABEL} (${MODE})"

# ── PATH for the service ──────────────────────────────────────────────────────
# launchd hands a job only /usr/bin:/bin:/usr/sbin:/sbin. Everything DevServer
# shells out to — node, npm, uv, git, claude, codex, pg_isready — lives outside
# that, so the PATH is pinned into the plist rather than discovered at runtime.
SERVICE_PATH="/opt/homebrew/bin:/opt/homebrew/sbin:${HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
for pg_bin in /opt/homebrew/opt/postgresql@*/bin /usr/local/opt/postgresql@*/bin; do
  [[ -d "$pg_bin" ]] && SERVICE_PATH="${SERVICE_PATH}:${pg_bin}"
done

# Report anything the service will need but cannot currently see.
missing=""
for tool in node npm uv git; do
  command -v "$tool" >/dev/null 2>&1 || missing="${missing} ${tool}"
done
[[ -n "$missing" ]] && yellow "  Warning: not on your PATH now:${missing}"

# ── Pre-build so the first login is not a five-minute wait ────────────────────
if [[ "$MODE" == "prod" && "$DO_BUILD" == "1" ]]; then
  if [[ ! -d "${WEB_DIR}/node_modules" ]]; then
    echo "  Installing npm dependencies..."
    npm --prefix "${WEB_DIR}" ci --prefer-offline
  fi
  echo "  Building Next.js (skip with --no-build)..."
  npm --prefix "${WEB_DIR}" run build
  green "  Build OK"
fi

chmod +x "${SCRIPT_DIR}/service-run.sh"
mkdir -p "${DS_LOG_DIR}" "$(dirname "$PLIST")"

# ── Write the plist ───────────────────────────────────────────────────────────
cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key><string>${LABEL}</string>

    <key>ProgramArguments</key>
    <array>
      <string>/bin/bash</string>
      <string>${SCRIPT_DIR}/service-run.sh</string>
      <string>--${MODE}</string>
    </array>

    <key>WorkingDirectory</key><string>${ROOT}</string>

    <!-- Start at login. Postgres is also a LaunchAgent and launchd orders
         neither, so service-run.sh waits for the database itself. -->
    <key>RunAtLoad</key><true/>

    <!-- Bring the pair back if either half dies. ThrottleInterval keeps a
         permanently broken config from spinning. -->
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>30</integer>

    <key>EnvironmentVariables</key>
    <dict>
      <key>PATH</key><string>${SERVICE_PATH}</string>
      <key>HOME</key><string>${HOME}</string>
      <key>DEVSERVER_ROOT</key><string>${ROOT}</string>
      <key>LANG</key><string>en_US.UTF-8</string>
    </dict>

    <!-- Supervisor narration only; the worker and web keep their own logs. -->
    <key>StandardOutPath</key><string>${DS_LOG_DIR}/service.log</string>
    <key>StandardErrorPath</key><string>${DS_LOG_DIR}/service.log</string>

    <key>ProcessType</key><string>Background</string>
  </dict>
</plist>
PLIST_EOF

plutil -lint "$PLIST" >/dev/null
green "  Wrote ${PLIST}"

# ── (Re)load ──────────────────────────────────────────────────────────────────
# Existing host processes would hold the ports the service is about to bind.
if port_busy "$WORKER_PORT" || port_busy "$WEB_PORT"; then
  yellow "  Stopping processes already on ${WORKER_PORT}/${WEB_PORT}..."
  stop_host >/dev/null 2>&1 || true
fi

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
# bootout is asynchronous: it returns before the job is actually gone, and
# bootstrapping while the old one still exists fails with "5: Input/output
# error". Wait for the label to disappear (exit timeout is 5s + child shutdown).
for _ in $(seq 1 30); do
  launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 || break
  sleep 1
done
launchctl bootstrap "$DOMAIN" "$PLIST"
launchctl enable "$DOMAIN/$LABEL"

green "  Loaded into ${DOMAIN}"
echo
bold "${LABEL} installed"
echo "  Starts:    at login (LaunchAgent), after Postgres is reachable"
echo "  Dashboard: http://localhost:${WEB_PORT}"
echo "  Logs:      ${DS_LOG_DIR}/service.log (supervisor), worker.log, web.log"
echo
echo "  Status:    launchctl print ${DOMAIN}/${LABEL} | head -20"
echo "  Restart:   launchctl kickstart -k ${DOMAIN}/${LABEL}"
echo "  Stop:      launchctl bootout ${DOMAIN}/${LABEL}"
echo "  Remove:    ./scripts/service-uninstall.sh"
