#!/bin/bash
# DevServer — foreground supervisor for launchd (macOS).
#
# Why this exists instead of pointing launchd at start.sh: start.sh nohups both
# processes and returns immediately, which launchd reads as "the job exited" and
# answers by restart-looping it forever. A launchd job must stay in the
# foreground for as long as the service is meant to be up. So this script keeps
# the worker and the web server as its own children, and only returns when one
# of them dies — at which point it takes the other down too and exits non-zero,
# letting the plist's KeepAlive bring the pair back cleanly.
#
# Ordering: PostgreSQL is a LaunchAgent as well (homebrew.mxcl.postgresql@NN)
# and launchd guarantees no ordering between agents, so this waits for the
# database to accept connections before starting anything. If Postgres never
# arrives we exit non-zero rather than starting a worker that cannot work —
# launchd retries after ThrottleInterval, which is the supervision loop doing
# its job.
#
# Logs: the children keep writing to logs/worker.log and logs/web.log, the same
# files the dashboard's Logs page tails. This script's own narration goes to
# logs/service.log via the plist.
#
# Usage (normally only launchd calls this):
#   scripts/service-run.sh [--prod|--dev]

# Deliberately no `set -e`: child failure is the condition this script exists to
# handle, not a reason to abort mid-supervision.
set -o pipefail

source "$(dirname "$0")/_lib.sh"

MODE="prod"
for arg in "$@"; do
  case "$arg" in
    --dev)  MODE="dev" ;;
    --prod) MODE="prod" ;;
  esac
done

PG_WAIT_SECONDS="${DEVSERVER_PG_WAIT:-180}"
POLL_SECONDS=3

log() { printf '%s [devserver.service] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

# ── Postgres readiness ────────────────────────────────────────────────────────

# pg_isready is not on the default launchd PATH (Homebrew keeps it in the
# versioned keg), so look there too before giving up on it.
find_pg_isready() {
  local candidate
  candidate="$(command -v pg_isready 2>/dev/null)"
  if [[ -n "$candidate" ]]; then echo "$candidate"; return; fi
  for candidate in /opt/homebrew/opt/postgresql@*/bin/pg_isready \
                   /usr/local/opt/postgresql@*/bin/pg_isready; do
    [[ -x "$candidate" ]] && { echo "$candidate"; return; }
  done
}

PG_ISREADY="$(find_pg_isready)"

# Host/port for the fallback probe, parsed out of DATABASE_URL.
pg_host_port() {
  local url="${DATABASE_URL:-}"
  local hostport="${url##*@}"
  hostport="${hostport%%/*}"
  local host="${hostport%%:*}"
  local port="${hostport##*:}"
  [[ "$host" == "$hostport" ]] && port=5432
  echo "${host:-127.0.0.1} ${port:-5432}"
}

pg_ready() {
  if [[ -n "$PG_ISREADY" ]]; then
    "$PG_ISREADY" -q -d "${DATABASE_URL:-}" >/dev/null 2>&1 && return 0
    return 1
  fi
  # No pg_isready anywhere — a raw TCP connect still tells us the postmaster
  # is listening, which is the part launchd ordering can get wrong.
  local host port
  read -r host port <<<"$(pg_host_port)"
  (exec 3<>"/dev/tcp/${host}/${port}") >/dev/null 2>&1 && { exec 3<&- 2>/dev/null; return 0; }
  return 1
}

wait_for_postgres() {
  if pg_ready; then
    log "Postgres is up."
    return 0
  fi
  log "Waiting for Postgres (up to ${PG_WAIT_SECONDS}s)..."
  local waited=0
  while (( waited < PG_WAIT_SECONDS )); do
    sleep 3
    waited=$(( waited + 3 ))
    if pg_ready; then
      log "Postgres is up after ${waited}s."
      return 0
    fi
  done
  log "ERROR: Postgres did not accept connections within ${PG_WAIT_SECONDS}s."
  log "       Check: brew services list | grep postgres"
  return 1
}

# ── Children ──────────────────────────────────────────────────────────────────

WORKER_PID=""
WEB_PID=""

stop_children() {
  local pid
  for pid in "$WEB_PID" "$WORKER_PID"; do
    [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null && kill -TERM "$pid" 2>/dev/null
  done
  # Give them a moment to shut down cleanly, then insist.
  local waited=0
  local alive=1
  while (( waited < 10 )); do
    alive=0
    for pid in "$WEB_PID" "$WORKER_PID"; do
      [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null && alive=1
    done
    (( alive == 0 )) && break
    sleep 1
    waited=$(( waited + 1 ))
  done
  if (( alive != 0 )); then
    for pid in "$WEB_PID" "$WORKER_PID"; do
      [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null
    done
  fi

  # Killing WEB_PID is not enough. In dev it is `npm run dev`, which spawns
  # cross-env -> tsx -> node and does not forward SIGTERM to that chain, so
  # npm dies while the real server keeps listening. The orphan then owns the
  # port when launchd restarts us, the fresh child cannot bind, it exits at
  # once, and the supervisor restart-loops against its own leftovers — the
  # "Port NNNN still held by a previous run" line in logs/service.log. The
  # ports are the only handle on a process we deliberately do not track, so
  # sweep them before returning rather than leaving it to the next startup.
  # kill_port only touches processes from this checkout, never a foreign
  # listener such as Docker Desktop's forwarder for a container on that port.
  local port
  for port in "$WEB_PORT" "$WORKER_PORT"; do
    if [[ -n "$(devserver_pids_on_port "$port")" ]]; then
      log "Releasing port ${port} held by an orphaned child."
      kill_port "$port"
    fi
  done
}

on_signal() {
  log "Signal received — shutting down."
  stop_children
  exit 0
}
trap on_signal TERM INT

start_worker() {
  if [[ ! -x "${WORKER_DIR}/.venv/bin/python" ]]; then
    log "ERROR: worker venv missing (${WORKER_DIR}/.venv). Run scripts/setup-local.sh."
    return 1
  fi
  # In dev the worker hot-reloads, same as start.sh. WORKER_PID is uvicorn's
  # reloader parent — it stays alive for the life of the service and passes
  # TERM to the app child, so supervision by pid still holds.
  # Scope the watcher to the worker's own source. Uvicorn's default is the
  # process cwd, which here is the repo root — ~37k files including
  # node_modules, apps/web/.next (rewritten on every dev request), logs/ that
  # the worker itself writes into, and worktrees/ that agents check out
  # mid-task. Watching all of that is pure overhead, and a stray .py landing
  # in a worktree restarts the worker out from under the task that created it.
  local reload_flag=""
  [[ "$MODE" == "dev" ]] && reload_flag="--reload --reload-dir ${WORKER_DIR}/src"
  # shellcheck disable=SC2086
  PYTHONPATH="${WORKER_DIR}/src" \
    "${WORKER_DIR}/.venv/bin/python" -m uvicorn src.main:app \
      --host "${WORKER_BIND:-127.0.0.1}" --port "${WORKER_PORT}" \
      --app-dir "${WORKER_DIR}" \
      ${reload_flag} \
      >> "${DS_LOG_DIR}/worker.log" 2>&1 &
  WORKER_PID=$!
  log "Worker started (pid ${WORKER_PID}) on :${WORKER_PORT}${reload_flag:+ (hot reload)}"
}

start_web() {
  if [[ ! -d "${WEB_DIR}/node_modules" ]]; then
    log "ERROR: ${WEB_DIR}/node_modules missing. Run: npm --prefix apps/web ci"
    return 1
  fi

  if [[ "$MODE" == "prod" ]]; then
    if [[ ! -f "${WEB_DIR}/.next/BUILD_ID" || ! -f "${WEB_DIR}/server.js" ]]; then
      log "No production build found — building (this delays first start)..."
      if ! npm --prefix "${WEB_DIR}" run build >> "${DS_LOG_DIR}/web.log" 2>&1; then
        log "ERROR: next build failed — see logs/web.log"
        return 1
      fi
      log "Build finished."
    fi
    # `exec` inside the subshell so $! is the real node process; without it we
    # would be holding the pid of a shell and TERM would leave node orphaned.
    ( cd "${WEB_DIR}" && exec env NODE_ENV=production node server.js ) \
      >> "${DS_LOG_DIR}/web.log" 2>&1 &
  else
    ( cd "${WEB_DIR}" && exec npm run dev ) \
      >> "${DS_LOG_DIR}/web.log" 2>&1 &
  fi
  WEB_PID=$!
  log "Web started (pid ${WEB_PID}) on :${WEB_PORT} (${MODE})"
}

# ── Main ──────────────────────────────────────────────────────────────────────

log "Starting DevServer (${MODE}) from ${ROOT}"

# A previous instance may have been killed without releasing its ports (hard
# reboot, SIGKILL). Clear them before binding, or the children die instantly
# and we restart-loop against ourselves. Only processes from this checkout are
# killed; a foreign holder is reported and left alone.
for port in "$WORKER_PORT" "$WEB_PORT"; do
  if port_busy "$port"; then
    if [[ -n "$(devserver_pids_on_port "$port")" ]]; then
      log "Port ${port} still held by a previous run — releasing."
      kill_port "$port"
    fi
    if port_busy "$port"; then
      log "ERROR: port ${port} is held by a non-DevServer process; not killing it:"
      describe_port_holders "$port"
      exit 1
    fi
  fi
done

wait_for_postgres || exit 1

start_worker || exit 1
start_web    || { stop_children; exit 1; }

log "DevServer is up — dashboard http://localhost:${WEB_PORT}"

# Supervise. bash 3.2 ships with macOS and has no `wait -n`, so poll.
while :; do
  if ! kill -0 "$WORKER_PID" 2>/dev/null; then
    log "Worker (pid ${WORKER_PID}) exited — see logs/worker.log. Restarting service."
    break
  fi
  if ! kill -0 "$WEB_PID" 2>/dev/null; then
    log "Web (pid ${WEB_PID}) exited — see logs/web.log. Restarting service."
    break
  fi
  sleep "$POLL_SECONDS"
done

stop_children
# Non-zero so launchd's KeepAlive brings the pair back together.
exit 1
