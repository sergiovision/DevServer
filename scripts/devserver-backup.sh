#!/bin/bash
# DevServer backup — exports database and .env into a single timestamped
# archive.
#
# Usage:
#   bash scripts/devserver-backup.sh [local|docker] [/path/to/output]
#
# Defaults:
#   mode   = local
#   output = ./backups/devserver-backup-YYYYMMDD-HHMMSS.tar.gz
#
# What gets backed up:
#   1. PostgreSQL full dump (pg_dump --format=custom)
#   2. Settings export (JSON snapshot from the settings table)
#   3. .env file(s) — the repo-root .env, plus docker/.env when it is a real
#      file rather than the setup-docker.sh symlink (secrets — handle the
#      archive with care)
#
# Worktrees and task logs are intentionally NOT backed up — they are
# reproducible/transient and dominate the archive size.
#
# The archive is self-contained: devserver-restore.sh can rebuild a full
# instance from it on a fresh machine.
set -euo pipefail

# _lib.sh: ROOT, repo-root .env loading, colour helpers, pg_cmd dispatch.
source "$(dirname "$0")/_lib.sh"

# Everything this script writes holds secrets (.env, docker/.env, the full DB
# dump): keep it owner-only from the first byte, staging directory included.
umask 077

MODE="${1:-local}"

# Docker deployments made by setup-release.sh keep their credentials in
# docker/.env (a real file), not the repo-root .env.
if [[ "$MODE" == "docker" && -f "${ROOT}/docker/.env" ]]; then
    set -a; source "${ROOT}/docker/.env"; set +a
fi
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
DEFAULT_OUTPUT="${ROOT}/backups/devserver-backup-${TIMESTAMP}.tar.gz"
OUTPUT="${2:-$DEFAULT_OUTPUT}"

# ── Paths ────────────────────────────────────────────────────────────────────
PGDATABASE="${PGDATABASE:-devserver}"
# Matches container_name in docker/docker-compose*.yml.
PG_CONTAINER="${PG_CONTAINER:-${DEVSERVER_STACK:-devserver}-postgres}"

psql_cmd()    { pg_cmd psql    "$PGDATABASE" "$@"; }
pg_dump_cmd() { pg_cmd pg_dump "$PGDATABASE" --format=custom "$@"; }

# ── Pre-flight ───────────────────────────────────────────────────────────────
bold "DevServer Backup"
echo "  Mode:      ${MODE}"
echo "  Database:  ${PGDATABASE}"
echo "  Output:    ${OUTPUT}"
echo ""

# Verify database is reachable
if ! psql_cmd -tAc "SELECT 1" >/dev/null 2>&1; then
    red "Cannot connect to database. Check your .env / PG* vars."
    exit 1
fi
green "  Database connection OK"

# ── Staging directory ────────────────────────────────────────────────────────
STAGING="$(mktemp -d)"
trap 'rm -rf "$STAGING"' EXIT

mkdir -p "$STAGING/db"
mkdir -p "$STAGING/meta"

# ── 1. Database dump ─────────────────────────────────────────────────────────
echo ""
bold "1/3  Dumping PostgreSQL..."
pg_dump_cmd > "$STAGING/db/devserver.dump"
DUMP_SIZE="$(du -h "$STAGING/db/devserver.dump" | cut -f1)"
green "  Database dump: ${DUMP_SIZE}"

# ── 2. Settings export (human-readable JSON) ─────────────────────────────────
bold "2/3  Exporting settings..."
psql_cmd -tAc "SELECT json_agg(row_to_json(s)) FROM settings s" \
    > "$STAGING/meta/settings.json" 2>/dev/null || echo "[]" > "$STAGING/meta/settings.json"
green "  Settings exported"

# ── 3. .env snapshot ─────────────────────────────────────────────────────────
bold "3/3  Copying .env..."
if [[ -f "${ROOT}/.env" ]]; then
    cp "${ROOT}/.env" "$STAGING/meta/dot-env"
    green "  .env copied"
else
    yellow "  No .env file found — skipping"
fi
# A symlinked docker/.env is just the root .env again — skip it.
if [[ -f "${ROOT}/docker/.env" && ! -L "${ROOT}/docker/.env" ]]; then
    cp "${ROOT}/docker/.env" "$STAGING/meta/docker-dot-env"
    green "  docker/.env copied"
fi

# ── Metadata ─────────────────────────────────────────────────────────────────
cat > "$STAGING/meta/backup-info.json" <<EOF
{
  "version": "1",
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "mode": "${MODE}",
  "database": "${PGDATABASE}",
  "hostname": "$(hostname)"
}
EOF

# ── Package ──────────────────────────────────────────────────────────────────
echo ""
bold "Packaging archive..."
OUT_DIR="$(dirname "$OUTPUT")"
mkdir -p "$OUT_DIR"
# The default backups/ directory is ours: owner-only. A caller-supplied
# directory (say /tmp) is left as it is — only the archive itself is locked down.
[[ "$OUT_DIR" == "${ROOT}/backups" ]] && chmod 700 "$OUT_DIR"
tar czf "$OUTPUT" -C "$STAGING" .
chmod 600 "$OUTPUT"
ARCHIVE_SIZE="$(du -h "$OUTPUT" | cut -f1)"

echo ""
green "Backup complete: ${OUTPUT} (${ARCHIVE_SIZE}, mode 600)"
echo ""
echo "Contents:"
echo "  db/devserver.dump       PostgreSQL custom-format dump"
echo "  meta/settings.json      Settings table (JSON)"
echo "  meta/dot-env            .env snapshot"
[[ -f "$STAGING/meta/docker-dot-env" ]] && echo "  meta/docker-dot-env     docker/.env snapshot"
echo "  meta/backup-info.json   Backup metadata"
echo ""
echo "Restore with:"
echo "  bash scripts/devserver-restore.sh ${MODE} ${OUTPUT}"
