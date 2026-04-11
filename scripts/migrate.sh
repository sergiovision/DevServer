#!/bin/bash
# Run database migrations
# Usage: ./scripts/migrate.sh [local|docker]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
MIGRATIONS_DIR="${PROJECT_ROOT}/database/migrations"

if [[ -f "${PROJECT_ROOT}/.env" ]]; then
    set -a
    source "${PROJECT_ROOT}/.env"
    set +a
fi

MODE="${1:-local}"

red()    { printf '\033[31m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }

OS_NAME="$(uname -s)"

psql_cmd() {
    if [[ "$MODE" == "docker" ]]; then
        docker exec -i devserver-postgres psql -U "${PGUSER:-devserver}" -d "${PGDATABASE:-devserver}" "$@"
    elif [[ -n "${PGHOST:-}" && -n "${PGUSER:-}" ]]; then
        # Use env vars (.env loaded above) — works on Linux and macOS.
        PGPASSWORD="${PGPASSWORD:-}" psql \
            -h "${PGHOST}" -p "${PGPORT:-5432}" \
            -U "${PGUSER}" -d "${PGDATABASE:-devserver}" "$@"
    elif [[ "$OS_NAME" == "Linux" ]]; then
        # Linux fallback: peer auth via postgres system user.
        sudo -u postgres psql -d "${PGDATABASE:-devserver}" "$@"
    else
        # macOS fallback: Homebrew PostgreSQL runs as the current user.
        psql -d "${PGDATABASE:-devserver}" "$@"
    fi
}

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
