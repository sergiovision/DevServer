#!/bin/bash
# Run database migrations
# Usage: ./scripts/migrate.sh [local|docker]
set -euo pipefail

# _lib.sh: ROOT, .env loading, colour helpers and the pg_cmd dispatch
# (docker container / PG* over TCP / peer auth) shared with backup + restore.
source "$(dirname "$0")/_lib.sh"
MIGRATIONS_DIR="${ROOT}/database/migrations"

MODE="${1:-local}"

psql_cmd() { pg_cmd psql "${PGDATABASE:-devserver}" "$@"; }

# Pre-flight: refuse to run if another connection holds locks on `tasks`.
# This catches the "idle in transaction" trap from a previously-killed worker
# that would otherwise make ALTER TABLE hang forever.
check_blockers() {
    local blockers
    blockers=$(psql_cmd -tAc "
        SELECT string_agg(pid::text || ' (' || state || ')', ', ')
        FROM pg_stat_activity
        WHERE datname = 'devserver'
          AND pid <> pg_backend_pid()
          AND state IN ('idle in transaction', 'idle in transaction (aborted)')
          AND query ILIKE '%tasks%';
    " 2>/dev/null || true)

    if [[ -n "$blockers" && "$blockers" != " " ]]; then
        red "Migration blocked: idle-in-transaction sessions hold locks on tasks:"
        red "  $blockers"
        yellow "  Terminate them, then retry:"
        yellow "    psql -h \"\${PGHOST}\" -U \"\${PGUSER}\" -d devserver -c \\"
        yellow "      \"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='devserver' AND state LIKE 'idle in transaction%';\""
        exit 1
    fi
}

echo "Running migrations (mode: ${MODE})..."
check_blockers

for f in "${MIGRATIONS_DIR}"/*.sql; do
    echo "  → $(basename "$f")"
    psql_cmd < "$f"
done

echo "Migrations complete."
