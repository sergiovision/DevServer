#!/bin/bash
# Shared helpers for DevServer start/stop/restart scripts.
# Source this file: `source "$(dirname "$0")/_lib.sh"`

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[1]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
DS_LOG_DIR="${ROOT}/logs"
WORKER_DIR="${ROOT}/apps/worker"
WEB_DIR="${ROOT}/apps/web"
DOCKER_DIR="${ROOT}/docker"

mkdir -p "${DS_LOG_DIR}/tasks"

# ── Env ───────────────────────────────────────────────────────────────────────
if [[ -f "${ROOT}/.env" ]]; then
  set -a; source "${ROOT}/.env"; set +a
fi

WORKER_PORT="${WORKER_PORT:-8000}"
WEB_PORT="${PORT:-3000}"

# ── Output ────────────────────────────────────────────────────────────────────
red()    { printf '\033[31m%s\033[0m\n' "$*"; }
green()  { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
bold()   { printf '\033[1m%s\033[0m\n' "$*"; }

# ── Mode parsing ──────────────────────────────────────────────────────────────
# Usage: MODE="$(parse_mode "$@")"
parse_mode() {
  local mode="dev"
  for arg in "$@"; do
    case "$arg" in
      --dev)    mode="dev" ;;
      --prod)   mode="prod" ;;
      --docker) mode="docker" ;;
    esac
  done
  echo "$mode"
}

# ── OS detection ──────────────────────────────────────────────────────────────
OS_NAME="$(uname -s)"
IS_MACOS=0
IS_LINUX=0
case "$OS_NAME" in
  Darwin) IS_MACOS=1 ;;
  Linux)  IS_LINUX=1 ;;
esac

# ── Process / port helpers ────────────────────────────────────────────────────
is_running() {
  local pid_file="$1"
  [[ -f "$pid_file" ]] && kill -0 "$(cat "$pid_file" 2>/dev/null)" 2>/dev/null
}

# List PIDs listening on a TCP port (cross-platform: prefers lsof, falls back to ss).
pids_on_port() {
  local port="$1"
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"${port}" -sTCP:LISTEN -t 2>/dev/null | sort -u
  elif command -v ss >/dev/null 2>&1; then
    ss -ltnp 2>/dev/null | awk -v p=":${port} " '$0 ~ p { while (match($0, /pid=[0-9]+/)) { print substr($0, RSTART+4, RLENGTH-4); $0 = substr($0, RSTART+RLENGTH) } }' | sort -u
  fi
}

# Kill anything listening on the given TCP port (best-effort, no error if free).
kill_port() {
  local port="$1"
  local pids
  pids="$(pids_on_port "$port")"
  if [[ -n "$pids" ]]; then
    # shellcheck disable=SC2086
    kill $pids 2>/dev/null || true
    sleep 1
    # shellcheck disable=SC2086
    kill -9 $pids 2>/dev/null || true
  fi
}

port_busy() {
  local port="$1"
  # Prefer `ss` — it lists listeners across all users without sudo. `lsof`
  # without root only sees the caller's own sockets, which hides host
  # PostgreSQL (running as the `postgres` user) from our detection.
  if command -v ss >/dev/null 2>&1; then
    ss -ltn 2>/dev/null | awk -v p=":${port}\$" '$4 ~ p { found=1 } END { exit !found }'
  elif command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"${port}" -sTCP:LISTEN >/dev/null 2>&1
  else
    # Last-resort probe via bash TCP
    (exec 3<>/dev/tcp/127.0.0.1/"${port}") 2>/dev/null && exec 3<&- 3>&-
  fi
}

# Sweep stray devserver processes spawned from the project tree.
#
# Targets BINARY paths inside the worker venv and the web node_modules — not the
# broad "apps/" substring — so we don't accidentally match an unrelated shell
# whose command line happens to mention the project path (e.g. a `pgrep` test).
#
# Matches things like:
#   /home/serg/devserver/apps/worker/.venv/bin/python3 ... uvicorn ...
#   node /home/serg/devserver/apps/web/node_modules/.bin/tsx server.ts
#   node --require .../apps/web/node_modules/tsx/dist/preflight.cjs ...
#
# Portable across Linux (procps pgrep) and macOS (BSD pgrep): the patterns are
# literal substrings except for `\.` which is a literal dot in both BRE and ERE.
sweep_orphans() {
  local patterns=(
    "${WORKER_DIR}/\\.venv/bin"
    "${WEB_DIR}/node_modules"
  )
  local signal pids p
  for signal in TERM KILL; do
    pids=""
    for p in "${patterns[@]}"; do
      pids+=" $(pgrep -f "$p" 2>/dev/null | grep -vx "$$" || true)"
    done
    pids="$(echo "$pids" | tr ' ' '\n' | sort -u | grep -v '^$' || true)"
    [[ -z "$pids" ]] && return 0
    # shellcheck disable=SC2086
    kill -"${signal}" $pids 2>/dev/null || true
    sleep 1
  done
}

# ── Docker helpers ────────────────────────────────────────────────────────────
docker_running() {
  docker ps --format '{{.Names}}' 2>/dev/null | grep -qE '^devserver-(web|worker|postgres)$'
}

# Decide whether to bring in the host-db override. Rules:
#   1. DEVSERVER_HOST_DB=1 forces host-db (explicit opt-in).
#   2. DEVSERVER_HOST_DB=0 forces bundled DB (explicit opt-out).
#   3. Otherwise, on Linux/macOS auto-detect: if something is already
#      listening on port 5432 on the host (not our own containers),
#      the bundled postgres would collide, so fall back to host-db.
#   4. Windows (Git Bash / WSL) always uses bundled.
docker_use_host_db() {
  case "${DEVSERVER_HOST_DB:-}" in
    1|true|yes) return 0 ;;
    0|false|no) return 1 ;;
  esac
  [[ "$IS_LINUX" == 1 || "$IS_MACOS" == 1 ]] || return 1
  local port="${PGPORT:-5432}"
  # Skip the detection if our own bundled-DB container is the one holding
  # the port — tearing down will free it and the user probably wants the
  # bundled DB back.
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx 'devserver-postgres'; then
    return 1
  fi
  port_busy "$port"
}

# Echoes the `-f` arguments for `docker compose` with the host-db override
# appended when appropriate. Call sites use:
#   docker compose $(docker_compose_files) up -d --build
docker_compose_files() {
  printf -- '-f %s ' "${DOCKER_DIR}/docker-compose.yml"
  if [[ -f "${DOCKER_DIR}/docker-compose.host-db.yml" ]] && docker_use_host_db; then
    printf -- '-f %s ' "${DOCKER_DIR}/docker-compose.host-db.yml"
  fi
}

docker_down() {
  if docker_running; then
    echo "Stopping docker stack..."
    # shellcheck disable=SC2046
    (cd "$DOCKER_DIR" && docker compose $(docker_compose_files) down) || return 1
    green "  Docker stack stopped"
  fi
}

# ── Host stop ─────────────────────────────────────────────────────────────────
# Tears down host worker + web exhaustively:
#   1. SIGTERM the tracked pid files
#   2. Free the dev ports (lsof/ss → kill)
#   3. Sweep any orphan spawned from the devserver tree (path-based)
#   4. Verify ports are actually free; SIGKILL anything still bound
stop_host() {
  local stopped=0

  # 1. Tracked pids
  for entry in "Worker:${DS_LOG_DIR}/worker.pid" "Web:${DS_LOG_DIR}/web.pid"; do
    local name="${entry%%:*}" pid_file="${entry#*:}"
    if [[ -f "$pid_file" ]]; then
      local pid; pid="$(cat "$pid_file" 2>/dev/null || true)"
      if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null || true
        green "  Stopped ${name} (pid ${pid})"
        stopped=1
      fi
      rm -f "$pid_file"
    fi
  done

  # 2 + 3. Always free ports and sweep orphans, regardless of pidfile state.
  #   The pidfile only tracks the immediate parent — `npm run dev` spawns
  #   `node tsx server.ts` which detaches and survives parent SIGTERM.
  if port_busy "$WORKER_PORT"; then kill_port "$WORKER_PORT"; stopped=1; fi
  if port_busy "$WEB_PORT";    then kill_port "$WEB_PORT";    stopped=1; fi
  if pgrep -f "${WORKER_DIR}/\\.venv/bin" >/dev/null 2>&1 \
     || pgrep -f "${WEB_DIR}/node_modules" >/dev/null 2>&1; then
    sweep_orphans
    stopped=1
  fi

  # 4. Verify. If anything is still bound, escalate to SIGKILL on the holders.
  local still_busy=0
  for port in "$WORKER_PORT" "$WEB_PORT"; do
    if port_busy "$port"; then
      local stragglers
      stragglers="$(pids_on_port "$port")"
      if [[ -n "$stragglers" ]]; then
        # shellcheck disable=SC2086
        kill -KILL $stragglers 2>/dev/null || true
        sleep 1
      fi
      port_busy "$port" && still_busy=1
    fi
  done

  if [[ "$still_busy" == 1 ]]; then
    red "Failed to free ports ${WORKER_PORT}/${WEB_PORT}. Holders:"
    pids_on_port "$WORKER_PORT"
    pids_on_port "$WEB_PORT"
    return 1
  fi

  [[ "$stopped" == 1 ]] && green "Host processes stopped" || echo "No host processes were running"
  return 0
}
