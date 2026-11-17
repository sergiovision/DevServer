#!/bin/bash
# DevServer restore — rebuilds a full instance from a backup archive
# created by devserver-backup.sh.
#
# Usage:
#   bash scripts/devserver-restore.sh [local|docker] /path/to/backup.tar.gz
#
# What gets restored:
#   1. PostgreSQL database (full drop + restore from pg_dump)
#   2. .env file(s) (optional — prompts before overwriting): the repo-root
#      .env and, if the backup has one, docker/.env
#
# Worktrees and task logs are not part of the backup archive (they are
# reproducible/transient), so they are not restored.
#
# Safety:
#   - Prompts before destructive operations (drop database, overwrite files)
#   - Snapshots the current database (if it exists) to
#     backups/pre-restore-<timestamp>.dump (mode 600) before dropping it, and
#     aborts if that snapshot fails
#   - Restores the database into the target shown at the prompt; the backup's
#     .env is written afterwards and never re-read by this script
#   - Any failed DROP / CREATE / pg_restore exits non-zero (pg_restore runs
#     as a single transaction, so a failure leaves an empty database, never a
#     half-restored one)
#   - Never force-overwrites .env without confirmation
#
# After restore, run:
#   bash scripts/migrate.sh        # apply any schema additions
#   bash scripts/start.sh          # start services
set -euo pipefail

# _lib.sh: ROOT, repo-root .env loading (if there is one — this may be a blank
# machine), colour helpers and the pg_cmd dispatch shared with backup/migrate.
source "$(dirname "$0")/_lib.sh"

MODE="${1:-local}"
ARCHIVE="${2:-}"

if [[ "$MODE" == "docker" && -f "${ROOT}/docker/.env" ]]; then
    set -a; source "${ROOT}/docker/.env"; set +a
fi

# Snapshots and restored .env files hold secrets: owner-only.
umask 077

# ── Validate arguments ───────────────────────────────────────────────────────
if [[ -z "$ARCHIVE" ]]; then
    red "Usage: bash scripts/devserver-restore.sh [local|docker] /path/to/backup.tar.gz"
    exit 1
fi

if [[ ! -f "$ARCHIVE" ]]; then
    red "Archive not found: $ARCHIVE"
    exit 1
fi

# ── Target (frozen here) ─────────────────────────────────────────────────────
# Everything below talks to the target resolved now and shown in the
# confirmation prompt. The backup's .env is restored only AFTER the database,
# and is never sourced, so it cannot redirect the restore to another database.
PGDATABASE="${PGDATABASE:-devserver}"
DB_OWNER="${PGUSER:-devserver}"
# Matches container_name in docker/docker-compose*.yml.
PG_CONTAINER="${PG_CONTAINER:-${DEVSERVER_STACK:-devserver}-postgres}"

# Names are interpolated into SQL below. Quote them as identifiers/literals
# AND refuse anything outside the plain-identifier alphabet, so a stray quote
# or semicolon in .env can never become part of a DROP DATABASE statement.
NAME_RE='^[A-Za-z_][A-Za-z0-9_$.-]{0,62}$'
for _name in "$PGDATABASE" "$DB_OWNER"; do
    if ! [[ "$_name" =~ $NAME_RE ]]; then
        red "Refusing unusual database/role name: '${_name}'"
        exit 1
    fi
done
sql_ident()   { printf '"%s"' "$(printf '%s' "$1" | sed 's/"/""/g')"; }
sql_literal() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/''/g")"; }
DB_IDENT="$(sql_ident "$PGDATABASE")"
DB_LITERAL="$(sql_literal "$PGDATABASE")"
OWNER_IDENT="$(sql_ident "$DB_OWNER")"

# stdin from /dev/null: `docker exec -i` would otherwise read the terminal and
# could swallow the answers to the prompts further down.
psql_admin_cmd() { pg_cmd psql postgres "$@" </dev/null; }
psql_cmd()       { pg_cmd psql "$PGDATABASE" "$@" </dev/null; }

# ── Extract archive ──────────────────────────────────────────────────────────
bold "DevServer Restore"
echo "  Mode:     ${MODE}"
echo "  Archive:  ${ARCHIVE}"
if [[ "$MODE" == "docker" ]]; then
    echo "  Target:   database '${PGDATABASE}' in container ${PG_CONTAINER}"
elif [[ -n "${PGHOST:-}" && -n "${PGUSER:-}" ]]; then
    echo "  Target:   database '${PGDATABASE}' on ${PGHOST}:${PGPORT:-5432} as ${PGUSER}"
else
    echo "  Target:   database '${PGDATABASE}' on the local server (peer auth)"
fi
echo ""

STAGING="$(mktemp -d)"
trap 'rm -rf "$STAGING"' EXIT

bold "Extracting archive..."
tar xzf "$ARCHIVE" -C "$STAGING"

# Show backup metadata
if [[ -f "$STAGING/meta/backup-info.json" ]]; then
    echo "  Backup info:"
    while IFS= read -r line; do echo "    $line"; done < "$STAGING/meta/backup-info.json"
    echo ""
fi

# Verify required files
if [[ ! -f "$STAGING/db/devserver.dump" ]]; then
    red "Archive is missing db/devserver.dump — not a valid DevServer backup."
    exit 1
fi

# ── Confirmation ─────────────────────────────────────────────────────────────
yellow "WARNING: This will:"
echo "  - Snapshot the current '${PGDATABASE}' database (if it exists) to backups/"
echo "  - Drop and recreate the '${PGDATABASE}' database"
echo ""
read -rp "Continue? [y/N] " confirm
if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
    echo "Aborted."
    exit 0
fi

# ── 1. Pre-restore snapshot ──────────────────────────────────────────────────
echo ""
bold "1/3  Pre-restore snapshot..."
if ! db_exists="$(psql_admin_cmd -tAc "SELECT 1 FROM pg_database WHERE datname = ${DB_LITERAL}")"; then
    red "  Cannot connect to the PostgreSQL server — nothing was changed."
    exit 1
fi

SNAPSHOT=""
if [[ "$(echo "$db_exists" | tr -d '[:space:]')" == "1" ]]; then
    mkdir -p "${ROOT}/backups"
    chmod 700 "${ROOT}/backups"
    SNAPSHOT="${ROOT}/backups/pre-restore-$(date +%Y%m%d-%H%M%S).dump"
    if ! pg_cmd pg_dump "$PGDATABASE" --format=custom </dev/null > "$SNAPSHOT"; then
        rm -f "$SNAPSHOT"
        red "  Snapshot of '${PGDATABASE}' failed — aborting before anything is dropped."
        exit 1
    fi
    chmod 600 "$SNAPSHOT"
    green "  Snapshot: ${SNAPSHOT} ($(du -h "$SNAPSHOT" | cut -f1))"
else
    yellow "  Database '${PGDATABASE}' does not exist yet — nothing to snapshot"
fi

# How to put the snapshot back, printed whenever a later step fails.
snapshot_hint() {
    if [[ -z "$SNAPSHOT" ]]; then return 0; fi
    yellow "  The previous database is saved in ${SNAPSHOT}. To put it back:"
    if [[ "$MODE" == "docker" ]]; then
        yellow "    docker exec -i ${PG_CONTAINER} pg_restore -U ${DB_OWNER} -d ${PGDATABASE} --no-owner --no-privileges --clean --if-exists < ${SNAPSHOT}"
    else
        yellow "    pg_restore -d ${PGDATABASE} --no-owner --no-privileges --clean --if-exists ${SNAPSHOT}"
    fi
}

# ── 2. Database restore ─────────────────────────────────────────────────────
echo ""
bold "2/3  Restoring database..."

# Terminate existing connections to allow the drop (best-effort: the DROP
# below reports the real error if a session survives).
psql_admin_cmd -tAc "
    SELECT pg_terminate_backend(pid)
    FROM pg_stat_activity
    WHERE datname = ${DB_LITERAL} AND pid <> pg_backend_pid();
" >/dev/null || true

if ! psql_admin_cmd -v ON_ERROR_STOP=1 -c "DROP DATABASE IF EXISTS ${DB_IDENT};"; then
    red "  DROP DATABASE failed (stop the worker/web first?) — the database was left as it was."
    exit 1
fi
if ! psql_admin_cmd -v ON_ERROR_STOP=1 -c "CREATE DATABASE ${DB_IDENT} OWNER ${OWNER_IDENT};"; then
    red "  CREATE DATABASE failed — '${PGDATABASE}' no longer exists."
    snapshot_hint
    exit 1
fi

# pgvector before the restore. The dump re-issues CREATE EXTENSION IF NOT
# EXISTS, so a failure here is only a warning; pg_restore reports the real one.
psql_cmd -c "CREATE EXTENSION IF NOT EXISTS vector;" >/dev/null \
    || yellow "  Could not create the vector extension up front (see above)"

# Into a FRESH database, so no --clean. --single-transaction + --exit-on-error
# make it all-or-nothing: the first error rolls back everything, rather than
# leaving a half-restored schema that a later count would call a success.
# pg_restore's stderr is shown as-is. The dump comes in on stdin, which works
# identically for the docker exec and host paths.
#
# Some pg_restore errors are harmless — typically "must be owner of extension"
# on a COMMENT ON EXTENSION when the restoring role is not a superuser. If the
# strict restore fails only on something like that, re-run with
# DEVSERVER_RESTORE_LENIENT=1: pg_restore then keeps going past errors, and the
# script reports them instead of aborting.
RESTORE_FLAGS=(--no-owner --no-privileges)
if [[ "${DEVSERVER_RESTORE_LENIENT:-0}" == "1" ]]; then
    yellow "  DEVSERVER_RESTORE_LENIENT=1 — pg_restore continues past errors"
else
    RESTORE_FLAGS+=(--single-transaction --exit-on-error)
fi
RESTORE_WARN=""
if ! pg_cmd pg_restore "$PGDATABASE" "${RESTORE_FLAGS[@]}" < "$STAGING/db/devserver.dump"; then
    if [[ "${DEVSERVER_RESTORE_LENIENT:-0}" == "1" ]]; then
        RESTORE_WARN=1
        yellow "  pg_restore reported errors (above) — review them before starting services."
    else
        red "  pg_restore failed (error above) — nothing was restored; '${PGDATABASE}' is empty."
        yellow "  If the error is harmless (e.g. 'must be owner of extension'), re-run with"
        yellow "  DEVSERVER_RESTORE_LENIENT=1 to restore past it."
        snapshot_hint
        exit 1
    fi
fi

TABLE_COUNT="$(psql_cmd -tAc "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'" || echo "?")"
green "  Database restored (${TABLE_COUNT} tables)"

# ── 3. .env restore ─────────────────────────────────────────────────────────
# After the database on purpose (see "Target" above), and not sourced: the
# restored values take effect the next time the services start.
echo ""
bold "3/3  Environment file..."
if [[ -f "$STAGING/meta/dot-env" ]]; then
    if [[ -f "${ROOT}/.env" ]]; then
        read -rp "  .env already exists. Overwrite with backup copy? [y/N] " env_confirm
        if [[ "$env_confirm" == "y" || "$env_confirm" == "Y" ]]; then
            cp "${ROOT}/.env" "${ROOT}/.env.pre-restore"
            chmod 600 "${ROOT}/.env.pre-restore"
            cp "$STAGING/meta/dot-env" "${ROOT}/.env"
            chmod 600 "${ROOT}/.env"
            green "  .env restored (previous saved as .env.pre-restore)"
        else
            yellow "  Keeping existing .env"
        fi
    else
        cp "$STAGING/meta/dot-env" "${ROOT}/.env"
        chmod 600 "${ROOT}/.env"
        green "  .env restored"
    fi
else
    yellow "  No .env in backup — skipping"
fi
if [[ -f "$STAGING/meta/docker-dot-env" ]]; then
    if [[ -L "${ROOT}/docker/.env" ]]; then
        yellow "  docker/.env is a symlink — leaving it (backup copy not restored)"
    elif [[ -f "${ROOT}/docker/.env" ]]; then
        read -rp "  docker/.env already exists. Overwrite with backup copy? [y/N] " denv_confirm
        if [[ "$denv_confirm" == "y" || "$denv_confirm" == "Y" ]]; then
            cp "${ROOT}/docker/.env" "${ROOT}/docker/.env.pre-restore"
            chmod 600 "${ROOT}/docker/.env.pre-restore"
            cp "$STAGING/meta/docker-dot-env" "${ROOT}/docker/.env"
            chmod 600 "${ROOT}/docker/.env"
            green "  docker/.env restored (previous saved as docker/.env.pre-restore)"
        else
            yellow "  Keeping existing docker/.env"
        fi
    else
        cp "$STAGING/meta/docker-dot-env" "${ROOT}/docker/.env"
        chmod 600 "${ROOT}/docker/.env"
        green "  docker/.env restored"
    fi
fi

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
if [[ -n "$RESTORE_WARN" ]]; then
    yellow "Restore complete, WITH pg_restore errors (see above)."
else
    green "Restore complete."
fi
echo ""
echo "Next steps:"
echo "  1. Run migrations to apply any schema updates:"
echo "       bash scripts/migrate.sh ${MODE}"
echo "  2. Start the services:"
if [[ "$MODE" == "docker" ]]; then
    echo "       bash scripts/start.sh --docker"
else
    echo "       bash scripts/start.sh            # or --prod"
fi
echo ""
echo "If you restored .env, verify the connection settings match your environment."
[[ -n "$SNAPSHOT" ]] && echo "Pre-restore snapshot: ${SNAPSHOT}"
exit 0
