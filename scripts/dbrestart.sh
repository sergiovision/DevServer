#!/bin/bash
# DevServer — restart PostgreSQL and verify it actually came back up.
#
# Works against whichever Postgres this deployment uses:
#   • the bundled `devserver-postgres` docker container
#   • a host service (macOS launchd / Homebrew, Linux systemd)
#   • a raw data directory driven by pg_ctl
#
# Also clears a stale `postmaster.pid` — the lock file left behind when a
# postmaster is killed mid-shutdown. Postgres then refuses to start with
# "lock file postmaster.pid already exists" and, under launchd/systemd
# KeepAlive, crash-loops forever.
#
# Usage:
#   ./scripts/dbrestart.sh
#
# Env overrides (also read from .env):
#   PGHOST PGPORT PGUSER PGPASSWORD PGDATABASE
#   PGDATA            explicit data directory for the pg_ctl fallback
#   DB_WAIT_SECONDS   readiness timeout (default 60)
set -euo pipefail

# Snapshot the caller's env BEFORE _lib.sh sources .env with `set -a`, which
# would otherwise clobber it. An explicit `PGPORT=5433 ./scripts/dbrestart.sh`
# has to win over the .env default — silently restarting and "verifying" a
# different server than the one asked for is worse than any error.
_CLI_PGHOST="${PGHOST:-}"
_CLI_PGPORT="${PGPORT:-}"
_CLI_PGUSER="${PGUSER:-}"
_CLI_PGPASSWORD="${PGPASSWORD:-}"
_CLI_PGDATABASE="${PGDATABASE:-}"
_CLI_PGDATA="${PGDATA:-}"

source "$(dirname "$0")/_lib.sh"

PG_HOST="${_CLI_PGHOST:-${PGHOST:-127.0.0.1}}"
PG_PORT="${_CLI_PGPORT:-${PGPORT:-5432}}"
PG_USER="${_CLI_PGUSER:-${PGUSER:-devserver}}"
PG_DB="${_CLI_PGDATABASE:-${PGDATABASE:-devserver}}"
PGPASSWORD="${_CLI_PGPASSWORD:-${PGPASSWORD:-}}"
PGDATA="${_CLI_PGDATA:-${PGDATA:-}}"
export PGPASSWORD
WAIT_SECONDS="${DB_WAIT_SECONDS:-60}"
PG_CONTAINER="${PG_CONTAINER:-devserver-postgres}"

# Filled in by detect_target(); one of docker|launchd|systemd|pgctl
TARGET=""
PGDATA_DIR=""
LAUNCHD_LABEL=""

# ── Client helpers ────────────────────────────────────────────────────────────
# psql/pg_isready may be absent on a docker-only host — fall back to the
# container's own binaries in that case.
have_local_client() { command -v pg_isready >/dev/null 2>&1 && command -v psql >/dev/null 2>&1; }

use_container_client() {
  [[ "$TARGET" == "docker" ]] && ! have_local_client
}

run_isready() {
  if use_container_client; then
    docker exec "$PG_CONTAINER" pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1
  else
    pg_isready -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1
  fi
}

# run_psql <sql> [dbname] — echoes a single unaligned value, empty on failure.
run_psql() {
  local sql="$1" db="${2:-$PG_DB}"
  if use_container_client; then
    docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$db" -tAc "$sql" 2>/dev/null || true
  else
    PGPASSWORD="${PGPASSWORD:-}" psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$db" \
      -tAc "$sql" 2>/dev/null || true
  fi
}

# ── Target detection ──────────────────────────────────────────────────────────
find_pgdata() {
  if [[ -n "${PGDATA:-}" && -f "${PGDATA}/PG_VERSION" ]]; then
    echo "$PGDATA"; return 0
  fi
  local brew_prefix d
  brew_prefix="$(brew --prefix 2>/dev/null || echo /opt/homebrew)"
  for d in "${brew_prefix}"/var/postgresql* /usr/local/var/postgresql* \
           /var/lib/postgresql/*/main /var/lib/pgsql/data; do
    [[ -f "${d}/PG_VERSION" ]] && { echo "$d"; return 0; }
  done
  return 1
}

detect_target() {
  # 1. Bundled container (running or stopped).
  if command -v docker >/dev/null 2>&1 \
     && docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$PG_CONTAINER"; then
    TARGET="docker"; return 0
  fi

  PGDATA_DIR="$(find_pgdata || true)"

  # 2. macOS launchd (Homebrew). Preferred over `brew services`, which breaks
  #    on some Homebrew versions but leaves a working launchd job behind.
  if [[ "$IS_MACOS" == 1 ]]; then
    LAUNCHD_LABEL="$(launchctl list 2>/dev/null | awk '$3 ~ /postgres/ { print $3 }' | head -1)"
    if [[ -z "$LAUNCHD_LABEL" ]]; then
      LAUNCHD_LABEL="$(ls "$HOME/Library/LaunchAgents" 2>/dev/null \
        | grep -i postgres | head -1 | sed 's/\.plist$//')"
    fi
    [[ -n "$LAUNCHD_LABEL" ]] && { TARGET="launchd"; return 0; }
  fi

  # 3. Linux systemd.
  if [[ "$IS_LINUX" == 1 ]] && command -v systemctl >/dev/null 2>&1 \
     && systemctl list-unit-files 2>/dev/null | grep -q '^postgresql'; then
    TARGET="systemd"; return 0
  fi

  # 4. Raw data directory.
  [[ -n "$PGDATA_DIR" ]] && { TARGET="pgctl"; return 0; }

  return 1
}

# ── Postmaster pid / stale lock cleanup ───────────────────────────────────────
# Echoes the pid from postmaster.pid only when it names a LIVE postgres
# process. Empty output means "no postmaster running" — either no lock file at
# all, or a stale one.
postmaster_pid() {
  local pidfile="${PGDATA_DIR}/postmaster.pid" pid comm
  [[ -n "$PGDATA_DIR" && -f "$pidfile" ]] || return 0
  pid="$(head -1 "$pidfile" 2>/dev/null || true)"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 0
  comm="$(ps -p "$pid" -o comm= 2>/dev/null || true)"
  [[ "$comm" == *postgres* ]] && echo "$pid"
  return 0
}

# The pid in postmaster.pid is stale when no live process holds it, or the
# process that does is not a postmaster (PIDs get recycled — that is how a
# crash-loop starts). A live postmaster is left strictly alone: under a
# KeepAlive supervisor it is the freshly relaunched server, not a leftover.
clear_stale_lock() {
  local pidfile="${PGDATA_DIR}/postmaster.pid" pid comm
  [[ -n "$PGDATA_DIR" && -f "$pidfile" ]] || return 0
  [[ -n "$(postmaster_pid)" ]] && return 0

  pid="$(head -1 "$pidfile" 2>/dev/null || true)"
  if [[ "$pid" =~ ^[0-9]+$ ]]; then
    comm="$(ps -p "$pid" -o comm= 2>/dev/null || true)"
    yellow "  Stale postmaster.pid (pid ${pid} is '${comm:-gone}', not a postmaster) — removing"
  else
    yellow "  Unreadable postmaster.pid — removing"
  fi
  rm -f "$pidfile"
}

# ps, not `kill -0`: kill -0 fails with EPERM for a process owned by another
# user (the `postgres` account on Linux), which would read as "already gone".
wait_pid_gone() {
  local pid="$1" secs="${2:-30}" i
  for i in $(seq 1 "$secs"); do
    ps -p "$pid" >/dev/null 2>&1 || return 0
    sleep 1
  done
  return 1
}

# ── Restart ───────────────────────────────────────────────────────────────────
maybe_sudo() {
  if [[ "$(id -u)" == 0 ]] || ! command -v sudo >/dev/null 2>&1; then
    "$@"
  else
    sudo "$@"
  fi
}

pg_ctl_bin() {
  command -v pg_ctl 2>/dev/null && return 0
  local c
  for c in /opt/homebrew/opt/postgresql*/bin/pg_ctl /usr/local/opt/postgresql*/bin/pg_ctl \
           /usr/lib/postgresql/*/bin/pg_ctl; do
    [[ -x "$c" ]] && { echo "$c"; return 0; }
  done
  return 1
}

# Wait for the port to be released after a stop, so the fresh postmaster
# doesn't lose the bind race with the one that's still shutting down.
wait_port_free() {
  local i
  for i in $(seq 1 15); do
    port_busy "$PG_PORT" || return 0
    sleep 1
  done
  return 1
}

restart_docker() {
  echo "Restarting container ${PG_CONTAINER}..."
  docker restart "$PG_CONTAINER" >/dev/null
}

restart_launchd() {
  local domain="gui/$(id -u)" old_pid
  echo "Restarting launchd job ${LAUNCHD_LABEL}..."

  # Track the OLD postmaster's pid, not the port. This job runs under
  # KeepAlive, so launchd relaunches postgres within the same second the old
  # one exits — the port is never observably free and waiting on it would
  # misread a successful restart as a hang.
  old_pid="$(postmaster_pid)"

  if [[ -n "$old_pid" ]]; then
    # SIGINT, not SIGTERM: to a postmaster SIGTERM means *smart* shutdown,
    # which waits for every client to disconnect — with a DevServer pool
    # attached that never happens and the server wedges in "shutting down".
    # SIGINT is the *fast* shutdown of `pg_ctl -m fast`: disconnect clients,
    # roll back their transactions, checkpoint, exit. (`kickstart -k` is not
    # usable for the same reason — launchd kills with SIGTERM.)
    launchctl kill SIGINT "${domain}/${LAUNCHD_LABEL}" >/dev/null 2>&1 || true
    if ! wait_pid_gone "$old_pid" 30; then
      # A second SIGINT promotes an in-progress smart shutdown to fast, which
      # also recovers a postmaster left wedged by an earlier plain SIGTERM.
      yellow "  Still shutting down — escalating to a second fast-shutdown signal"
      launchctl kill SIGINT "${domain}/${LAUNCHD_LABEL}" >/dev/null 2>&1 || true
      wait_pid_gone "$old_pid" 15 || {
        red "  Old postmaster (pid ${old_pid}) would not shut down"
        return 1
      }
    fi
    green "  Old postmaster (pid ${old_pid}) stopped"
  fi

  # Clear the lock only if it is stale; KeepAlive has usually relaunched
  # postgres already, in which case start is a harmless no-op.
  clear_stale_lock
  launchctl kickstart "${domain}/${LAUNCHD_LABEL}" >/dev/null 2>&1 \
    || launchctl start "$LAUNCHD_LABEL" >/dev/null 2>&1 || true
}

restart_systemd() {
  local unit
  unit="$(systemctl list-unit-files 2>/dev/null | awk '/^postgresql/ { print $1; exit }')"
  unit="${unit:-postgresql}"
  echo "Restarting systemd unit ${unit}..."
  # `systemctl stop` is synchronous and the postgres units ship
  # KillSignal=SIGINT, so this is already a fast shutdown.
  maybe_sudo systemctl stop "$unit" >/dev/null 2>&1 || true
  wait_port_free || true
  clear_stale_lock
  maybe_sudo systemctl start "$unit"
}

restart_pgctl() {
  local bin
  bin="$(pg_ctl_bin)" || { red "pg_ctl not found"; return 1; }
  echo "Restarting postgres in ${PGDATA_DIR} via pg_ctl..."
  "$bin" -D "$PGDATA_DIR" -m fast stop >/dev/null 2>&1 || true
  wait_port_free || true
  clear_stale_lock
  "$bin" -D "$PGDATA_DIR" -l "${DS_LOG_DIR}/postgres.log" start >/dev/null
}

# ── Verify ────────────────────────────────────────────────────────────────────
wait_ready() {
  local i
  for i in $(seq 1 "$WAIT_SECONDS"); do
    if run_isready; then
      green "  Accepting connections after ${i}s"
      return 0
    fi
    sleep 1
  done
  return 1
}

show_logs() {
  local f
  for f in /opt/homebrew/var/log/postgresql*.log /usr/local/var/log/postgresql*.log \
           /var/log/postgresql/*.log; do
    [[ -f "$f" ]] || continue
    yellow "Last lines of ${f}:"
    tail -n 15 "$f"
    return
  done
  if [[ "$TARGET" == "docker" ]]; then
    yellow "Last lines of ${PG_CONTAINER} logs:"
    docker logs --tail 15 "$PG_CONTAINER" 2>&1 || true
  fi
}

# ── Main ──────────────────────────────────────────────────────────────────────
bold "Restarting PostgreSQL (${PG_USER}@${PG_HOST}:${PG_PORT}/${PG_DB})"

if ! detect_target; then
  red "Could not find a PostgreSQL to restart."
  echo "  Looked for: docker container '${PG_CONTAINER}', a launchd/systemd service, and a data directory."
  echo "  Set PGDATA to the data directory if Postgres is installed somewhere non-standard."
  exit 1
fi

echo "  Target: ${TARGET}${PGDATA_DIR:+ (${PGDATA_DIR})}"

case "$TARGET" in
  docker)  restart_docker  ;;
  launchd) restart_launchd ;;
  systemd) restart_systemd ;;
  pgctl)   restart_pgctl   ;;
esac || { red "Restart command failed"; show_logs; exit 1; }

echo "Waiting for readiness (timeout ${WAIT_SECONDS}s)..."
if ! wait_ready; then
  red "PostgreSQL did not accept connections within ${WAIT_SECONDS}s"
  show_logs
  exit 1
fi

# Server is up — now prove the app database is actually usable.
VERSION="$(run_psql 'SHOW server_version')"
if [[ -z "$VERSION" ]]; then
  red "Server is listening but a query against database '${PG_DB}' as '${PG_USER}' failed."
  echo "  Check PGUSER/PGPASSWORD/PGDATABASE in .env, or run ./scripts/migrate.sh to create the database."
  exit 1
fi
green "  Connected — PostgreSQL ${VERSION}"

# Informational: schema state. A restart is still a success without these.
TABLES="$(run_psql "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")"
echo "  Tables in public schema: ${TABLES:-unknown}"
if [[ "${TABLES:-0}" -eq 0 ]]; then
  yellow "  Database is empty — run ./scripts/migrate.sh to apply migrations"
fi

VECTOR="$(run_psql "SELECT extversion FROM pg_extension WHERE extname='vector'")"
if [[ -n "$VECTOR" ]]; then
  echo "  pgvector: ${VECTOR}"
else
  yellow "  pgvector extension not installed (memory/corpus features need it)"
fi

green "PostgreSQL restarted and verified"
