#!/bin/bash
# DevServer — start in dev (default), prod, or docker mode.
#
# Usage:
#   ./scripts/start.sh              # dev mode (hot reload)
#   ./scripts/start.sh --dev         # dev mode (hot reload)
#   ./scripts/start.sh --prod        # prod mode (build + node server.js)
#   ./scripts/start.sh --docker      # docker compose up -d --build
#
# Logs: logs/worker.log, logs/web.log
# PIDs: logs/worker.pid, logs/web.pid
set -euo pipefail

source "$(dirname "$0")/_lib.sh"
MODE="$(parse_mode "$@")"

# ── Pre-flight ────────────────────────────────────────────────────────────────
preflight() {
  if [[ "$MODE" == "docker" ]]; then
    if port_busy "$WEB_PORT" || port_busy "$WORKER_PORT"; then
      red "Ports ${WEB_PORT}/${WORKER_PORT} are in use by host processes."
      yellow "  Run: ./scripts/stop.sh   (then try again)"
      exit 1
    fi
  else
    if docker_running; then
      red "DevServer docker stack is already running."
      yellow "  Run: ./scripts/restart.sh --${MODE}   (switches modes cleanly)"
      exit 1
    fi
    if port_busy "$WEB_PORT" || port_busy "$WORKER_PORT"; then
      red "Ports ${WEB_PORT}/${WORKER_PORT} are already in use."
      yellow "  Run: ./scripts/stop.sh   (then try again)"
      exit 1
    fi
  fi
}

# ── Worker (host) ─────────────────────────────────────────────────────────────
start_worker() {
  echo "Starting worker (${MODE})..."

  # Create venv if missing: prefer `uv venv` (project standard), else python3 -m venv.
  if [[ ! -f "${WORKER_DIR}/.venv/bin/activate" ]]; then
    echo "  Creating Python venv..."
    if command -v uv >/dev/null 2>&1; then
      (cd "${WORKER_DIR}" && uv venv)
    else
      python3 -m venv "${WORKER_DIR}/.venv"
    fi
  fi

  # Install/refresh the worker package. Always invoke tools via the venv's
  # python (`python -m …`) or uv — never the `.venv/bin/pip` wrapper directly:
  # its shebang hardcodes the venv's original absolute path, so a moved/copied
  # checkout breaks `bin/pip` with "cannot execute: required file not found"
  # even though the file is present and executable. `uv` (project standard) is
  # preferred; `python -m pip` is the robust fallback; ensurepip the last resort.
  if command -v uv >/dev/null 2>&1; then
    (cd "${WORKER_DIR}" && VIRTUAL_ENV="${WORKER_DIR}/.venv" uv pip install -q -e .)
  elif "${WORKER_DIR}/.venv/bin/python" -m pip --version >/dev/null 2>&1; then
    "${WORKER_DIR}/.venv/bin/python" -m pip install -q -e "${WORKER_DIR}"
  else
    # Last resort: bootstrap pip into the venv, then install.
    "${WORKER_DIR}/.venv/bin/python" -m ensurepip --upgrade >/dev/null 2>&1 || {
      red "Neither uv nor pip is available in the worker venv. Install uv or recreate the venv with 'python3 -m venv'."
      exit 1
    }
    "${WORKER_DIR}/.venv/bin/python" -m pip install -q -e "${WORKER_DIR}"
  fi

  local reload_flag=""
  [[ "$MODE" == "dev" ]] && reload_flag="--reload"

  # Invoke uvicorn via `python -m` (not the .venv/bin/uvicorn wrapper) so a
  # stale console-script shebang in a moved venv can't break the launch.
  PYTHONPATH="${WORKER_DIR}/src" \
    nohup "${WORKER_DIR}/.venv/bin/python" -m uvicorn src.main:app \
      --host 0.0.0.0 --port "${WORKER_PORT}" \
      --app-dir "${WORKER_DIR}" \
      ${reload_flag} \
    >> "${DS_LOG_DIR}/worker.log" 2>&1 &
  disown
  echo $! > "${DS_LOG_DIR}/worker.pid"
  green "  Worker started (pid $(cat "${DS_LOG_DIR}/worker.pid")) → logs/worker.log"
}

# ── Web (host) ────────────────────────────────────────────────────────────────
start_web() {
  echo "Starting web (${MODE})..."

  if [[ ! -d "${WEB_DIR}/node_modules" ]]; then
    echo "  Installing npm dependencies..."
    npm --prefix "${WEB_DIR}" ci --prefer-offline
  fi

  # setsid is Linux-only; on macOS nohup + disown in a subshell achieves the same detach.
  local setsid_cmd=""
  if command -v setsid >/dev/null 2>&1; then
    setsid_cmd="setsid"
  fi

  if [[ "$MODE" == "prod" ]]; then
    echo "  Building Next.js..."
    npm --prefix "${WEB_DIR}" run build
    (cd "${WEB_DIR}" && ${setsid_cmd} nohup env NODE_ENV=production node server.js \
      </dev/null >> "${DS_LOG_DIR}/web.log" 2>&1 &
      echo $! > "${DS_LOG_DIR}/web.pid"
      disown 2>/dev/null || true)
  else
    # dev: tsx server.ts via npm — setsid on Linux keeps the loader alive after shell exit
    (cd "${WEB_DIR}" && ${setsid_cmd} nohup npm run dev \
      </dev/null >> "${DS_LOG_DIR}/web.log" 2>&1 &
      echo $! > "${DS_LOG_DIR}/web.pid"
      disown 2>/dev/null || true)
  fi
  green "  Web started (pid $(cat "${DS_LOG_DIR}/web.pid")) → logs/web.log"
}

# ── Docker ────────────────────────────────────────────────────────────────────
start_docker() {
  echo "Starting docker stack..."
  if docker_is_host_worker; then
    echo "  Topology: worker on host (Postgres + web in Docker)"
  elif docker_use_host_db; then
    yellow "  Using host PostgreSQL (override: docker-compose.host-db.yml)"
  fi
  if docker_use_bundled_db; then
    echo "  Database: bundled PostgreSQL container"
  else
    yellow "  Database: external / host PostgreSQL (bundled container skipped)"
  fi
  # shellcheck disable=SC2046
  (cd "$DOCKER_DIR" && docker compose $(docker_compose_files) $(docker_compose_profiles) up -d --build)
  green "  Docker stack up"

  # In the host-worker topology the worker is NOT a container — start it on
  # the host so it can drive the AI provider CLIs with the host's own logins.
  if docker_is_host_worker; then
    : > "${DS_LOG_DIR}/worker.log"
    start_worker
  fi
}

# ── Run ───────────────────────────────────────────────────────────────────────
preflight

case "$MODE" in
  dev|prod)
    : > "${DS_LOG_DIR}/worker.log"
    : > "${DS_LOG_DIR}/web.log"
    start_worker
    start_web
    ;;
  docker)
    start_docker
    ;;
esac

echo ""
bold "DevServer running (${MODE})"
echo "  Dashboard:   http://localhost:${WEB_PORT}"
echo "  Worker API:  http://localhost:${WORKER_PORT}"
if [[ "$MODE" == "docker" ]]; then
  if docker_is_host_worker; then
    echo "  Worker log:  tail -f logs/worker.log  (worker runs on host)"
  fi
else
  echo "  Worker log:  tail -f logs/worker.log"
  echo "  Web log:     tail -f logs/web.log"
fi
echo "  Stop:        ./scripts/stop.sh --${MODE}"
