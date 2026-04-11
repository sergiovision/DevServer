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

  # Install/refresh the worker package. `uv venv` does not include pip, so prefer uv.
  if [[ -x "${WORKER_DIR}/.venv/bin/pip" ]]; then
    "${WORKER_DIR}/.venv/bin/pip" install -q -e "${WORKER_DIR}"
  elif command -v uv >/dev/null 2>&1; then
    (cd "${WORKER_DIR}" && VIRTUAL_ENV="${WORKER_DIR}/.venv" uv pip install -q -e .)
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

  PYTHONPATH="${WORKER_DIR}/src" \
    nohup "${WORKER_DIR}/.venv/bin/uvicorn" src.main:app \
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
  (cd "$DOCKER_DIR" && docker compose up -d --build)
  green "  Docker stack up"
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
if [[ "$MODE" != "docker" ]]; then
  echo "  Worker log:  tail -f logs/worker.log"
  echo "  Web log:     tail -f logs/web.log"
fi
echo "  Stop:        ./scripts/stop.sh --${MODE}"
