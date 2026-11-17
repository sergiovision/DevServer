#!/usr/bin/env bash
# DevServer — pull the published images from Docker Hub and start the stack.
#
#   bash scripts/rundockerhub.sh                 # both tiers at *-latest
#   bash scripts/rundockerhub.sh 23-406fcbc      # pin both tiers to one build
#
# Builds nothing. This is the "run a published image" path: it pulls
# ${DEVSERVER_IMAGE} (default dockersergio/devserver) and brings up
# docker/docker-compose.release.yml.
#
# The counterpart scripts are setup-release.sh, which ASKS the deployment
# questions once and writes docker/.env, and setup-docker.sh, which builds
# from this checkout instead of pulling. Run setup-release.sh first; run this
# one every time after, to move onto a newer published image.
#
# Four things it does that a bare `docker compose pull && up -d` does not:
#
#   1. Pins an explicit `-p devserver` project name. Without it Compose names
#      the project after the directory (`docker`), so the dev and release
#      files resolve to the SAME volume names and quietly share state.
#   2. Refuses to start a tier on the other tier's tag. The two images are
#      separate artifacts now; crossed tags would still come up "healthy",
#      because each image would happily probe itself.
#   3. Repairs the log volume's ownership. Images from before the non-root
#      switch ran as root, so a volume created back then is root-owned and
#      the current uid 10001 cannot write to it — both tiers then log "/app/logs is not writable"
#      and the in-app /logs viewer shows nothing.
#   4. Waits for the worker to answer /health. The worker applies migrations
#      on an empty schema before it serves, so healthy here genuinely means
#      ready, not merely listening.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
DOCKER_DIR="${PROJECT_ROOT}/docker"
COMPOSE_FILE="${DOCKER_DIR}/docker-compose.release.yml"
ENV_FILE="${DOCKER_DIR}/.env"
# Caller's DEVSERVER_STACK wins; otherwise the one setup-release.sh wrote to
# docker/.env (re-read below, after that file is sourced).
_CLI_STACK="${DEVSERVER_STACK:-}"
PROJECT_NAME="${DEVSERVER_STACK:-devserver}"

c_reset=$'\033[0m'; c_bold=$'\033[1m'; c_dim=$'\033[2m'
c_green=$'\033[32m'; c_yellow=$'\033[33m'; c_red=$'\033[31m'; c_cyan=$'\033[36m'

hd()   { printf '\n%s%s%s\n' "$c_bold" "$*" "$c_reset"; }
ok()   { printf '  %s✓%s %s\n' "$c_green" "$c_reset" "$*"; }
warn() { printf '  %s!%s %s\n' "$c_yellow" "$c_reset" "$*"; }
err()  { printf '  %s✗%s %s\n' "$c_red" "$c_reset" "$*" >&2; }
note() { printf '    %s%s%s\n' "$c_dim" "$*" "$c_reset"; }

usage() {
    sed -n '2,8p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

BUILD=""
for arg in "$@"; do
    case "$arg" in
        -h|--help) usage 0 ;;
        -*)        err "unknown option: $arg"; usage 1 ;;
        *)         BUILD="$arg" ;;
    esac
done

# ── Prerequisites ────────────────────────────────────────────────────────────
hd "DevServer — run published images"

command -v docker >/dev/null 2>&1 || { err "docker not found — https://docs.docker.com/get-docker/"; exit 1; }
docker compose version >/dev/null 2>&1 || { err "docker compose plugin not found"; exit 1; }
docker info >/dev/null 2>&1 || { err "Docker daemon is not running — start Docker Desktop and re-run."; exit 1; }
[[ -f "$COMPOSE_FILE" ]] || { err "missing $COMPOSE_FILE"; exit 1; }
ok "docker $(docker --version | awk '{print $3}' | tr -d ,), compose $(docker compose version --short)"

# docker/.env carries the database credentials and ports. Without it the
# compose defaults apply, which means PGPASSWORD=changeme — fine for a look
# around, wrong for anything kept.
if [[ -f "$ENV_FILE" ]]; then
    set -a; source "$ENV_FILE"; set +a
    PROJECT_NAME="${_CLI_STACK:-${DEVSERVER_STACK:-devserver}}"
    ok "read ${ENV_FILE#"$PROJECT_ROOT"/}"
else
    warn "no docker/.env — falling back to compose defaults (PGPASSWORD=changeme)"
    note "Run 'bash scripts/setup-release.sh' once to generate it properly."
fi

# ── Tags ─────────────────────────────────────────────────────────────────────
# One repository, one tag per tier. A positional argument pins both tiers to
# the same build, which is the only combination that is reproducible: the pair
# is built from one commit, and compose deploys them together.
DEVSERVER_IMAGE="${DEVSERVER_IMAGE:-dockersergio/devserver}"
if [[ -n "$BUILD" ]]; then
    DEVSERVER_WORKER_VERSION="worker-${BUILD}"
    DEVSERVER_WEB_VERSION="web-${BUILD}"
else
    DEVSERVER_WORKER_VERSION="${DEVSERVER_WORKER_VERSION:-worker-latest}"
    DEVSERVER_WEB_VERSION="${DEVSERVER_WEB_VERSION:-web-latest}"
fi
# Moving pointers must re-resolve every run, or `up` keeps whatever layers
# this host happens to have cached and "latest" silently means "last week".
case "$DEVSERVER_WORKER_VERSION$DEVSERVER_WEB_VERSION" in
    *latest*) DEVSERVER_PULL_POLICY="always" ;;
    *)        DEVSERVER_PULL_POLICY="${DEVSERVER_PULL_POLICY:-missing}" ;;
esac
export DEVSERVER_IMAGE DEVSERVER_WORKER_VERSION DEVSERVER_WEB_VERSION DEVSERVER_PULL_POLICY

# Crossing the tiers is the one mistake left that would look healthy, because
# each image carries its own CMD and HEALTHCHECK and would probe itself.
[[ "$DEVSERVER_WORKER_VERSION" == worker-* ]] || { err "worker tier is on '${DEVSERVER_WORKER_VERSION}' — expected a worker-* tag"; exit 1; }
[[ "$DEVSERVER_WEB_VERSION"    == web-*    ]] || { err "web tier is on '${DEVSERVER_WEB_VERSION}' — expected a web-* tag"; exit 1; }

ok "worker  ${DEVSERVER_IMAGE}:${DEVSERVER_WORKER_VERSION}"
ok "web     ${DEVSERVER_IMAGE}:${DEVSERVER_WEB_VERSION}"

# ── Database topology ────────────────────────────────────────────────────────
# The bundled Postgres sits behind a compose profile. It should run unless the
# deployment points somewhere else — which setup-release.sh records by writing
# a PGHOST_CONTAINER other than the `postgres` service name.
BUNDLED_DB=1
case "${DEVSERVER_HOST_DB:-}" in
    1|true|yes) BUNDLED_DB=0 ;;
    *) [[ "${PGHOST_CONTAINER:-postgres}" == "postgres" ]] || BUNDLED_DB=0 ;;
esac

port_free() { ! (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

# The profile is appended to COMPOSE rather than kept in its own array: macOS
# ships bash 3.2, where expanding an EMPTY array under `set -u` is a fatal
# "unbound variable" — so a separate, sometimes-empty PROFILE array would kill
# this script on exactly the external-database path it is meant to support.
COMPOSE=(docker compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE")
PROFILE_NOTE=""

if [[ "$BUNDLED_DB" == 1 ]]; then
    # The bundled DB publishes a host port so psql, the backup/restore pair and
    # a host-side worker can reach it. The two tiers do NOT use it — they talk
    # to the `postgres` service over the compose network — so when 5432 already
    # belongs to a PostgreSQL on this host, moving the published port aside is
    # strictly better than failing the whole stack on a bind error.
    PGPORT="${PGPORT:-5432}"
    if ! port_free "$PGPORT"; then
        taken="$PGPORT"
        for cand in $(seq $((taken + 1)) $((taken + 20))); do
            if port_free "$cand"; then PGPORT="$cand"; break; fi
        done
        if [[ "$PGPORT" == "$taken" ]]; then
            err "port ${taken} is in use and no free port found in $((taken + 1))-$((taken + 20))"
            note "Set PGBIND/PGPORT in docker/.env, or stop whatever holds ${taken}."
            exit 1
        fi
        warn "host port ${taken} is already in use — publishing the bundled DB on ${PGPORT} instead"
        note "Containers are unaffected; this only changes the host-side port."
    fi
    export PGPORT
    COMPOSE+=(--profile bundled-db)
    PROFILE_NOTE="--profile bundled-db "
    ok "database: bundled pgvector container (host port ${PGPORT})"
else
    ok "database: external — ${PGHOST_CONTAINER:-?}:${PGPORT_CONTAINER:-5432}"
fi

# ── Pull ─────────────────────────────────────────────────────────────────────
hd "Pulling"
if ! (cd "$DOCKER_DIR" && "${COMPOSE[@]}" pull -q); then
    err "pull failed"
    note "Private repository? Run: docker login"
    note "Tag missing? Both tiers are published together; check the pair exists:"
    note "  docker manifest inspect ${DEVSERVER_IMAGE}:${DEVSERVER_WORKER_VERSION}"
    exit 1
fi
ok "pulled"

# ── Repair root-owned volumes from older images ───────────────────────────────
# Only touches a volume that is actually wrong, and only ever hands it to the
# uid the image runs as. A volume created by a root-running image keeps its
# root ownership forever otherwise — Docker populates a named volume from the
# image only when the volume is empty, so a newer image never fixes it.
fix_volume_owner() {
    local vol="${PROJECT_NAME}_$1" owner
    docker volume inspect "$vol" >/dev/null 2>&1 || return 0
    owner="$(docker run --rm --user 0 -v "${vol}:/v" \
        "${DEVSERVER_IMAGE}:${DEVSERVER_WORKER_VERSION}" stat -c %u /v 2>/dev/null || echo 10001)"
    [[ "$owner" == "10001" ]] && return 0
    warn "volume ${vol} is owned by uid ${owner}; the image runs as 10001"
    docker run --rm --user 0 -v "${vol}:/v" \
        "${DEVSERVER_IMAGE}:${DEVSERVER_WORKER_VERSION}" chown -R 10001:10001 /v >/dev/null 2>&1 \
        && ok "chowned ${vol} to 10001:10001" \
        || warn "could not chown ${vol} — /logs may stay empty"
}
fix_volume_owner worker_logs
fix_volume_owner worktrees

# ── Start ────────────────────────────────────────────────────────────────────
hd "Starting"
(cd "$DOCKER_DIR" && "${COMPOSE[@]}" up -d)

WORKER_HOST_PORT="${WORKER_HOST_PORT:-8000}"
WEB_PORT="${WEB_PORT:-3200}"

printf '\n  waiting for the worker'
healthy=0
for _ in $(seq 1 40); do
    if curl -fsS -m 3 "http://127.0.0.1:${WORKER_HOST_PORT}/health" >/dev/null 2>&1; then healthy=1; break; fi
    printf '.'; sleep 3
done
echo

# ── Report ───────────────────────────────────────────────────────────────────
hd "Status"
(cd "$DOCKER_DIR" && "${COMPOSE[@]}" ps --format 'table {{.Name}}\t{{.Image}}\t{{.Status}}')

if [[ "$healthy" == 1 ]]; then
    ok "worker healthy"
else
    err "worker did not become healthy"
    note "Compose reports only 'dependency failed to start'; the real cause is in the log:"
    note "  docker logs ${PROJECT_NAME}-worker"
    exit 1
fi

hd "Done"
printf '  Dashboard   %shttp://localhost:%s%s\n' "$c_cyan" "$WEB_PORT" "$c_reset"
printf '  Worker API  http://127.0.0.1:%s\n' "$WORKER_HOST_PORT"
echo
note "First visit redirects once to /setup, which reads the database and then"
note "sets the cookie the dashboard checks. Settings has a Setup button too."
echo
note "logs:  docker compose -p ${PROJECT_NAME} -f docker/docker-compose.release.yml logs -f"
note "stop:  docker compose -p ${PROJECT_NAME} -f docker/docker-compose.release.yml ${PROFILE_NOTE}down"
