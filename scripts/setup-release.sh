#!/usr/bin/env bash
# DevServer — interactive setup for PREBUILT Docker images.
#
#   bash scripts/setup-release.sh
#
# The counterpart to setup-docker.sh, which builds from this checkout. This one
# builds nothing: it pulls ${DEVSERVER_IMAGE} (default dockersergio/devserver
# on Docker Hub, or your own registry) and runs docker/docker-compose.release.yml,
# so it works on a host that has the compose file and nothing else.
#
# It asks the handful of questions that actually change the deployment —
# which database, which tag, which ports, which agent credentials — writes
# docker/.env, and brings the stack up.
#
# Non-interactive (CI): set DEVSERVER_SETUP_YES=1 and any of the vars below to
# accept every default without prompting.
#
# Three things it deliberately does differently from setup-docker.sh, each
# because it went wrong in practice:
#
#   1. docker/.env is written as a REAL FILE, never a symlink to the repo-root
#      .env. setup-docker.sh symlinks them, so anything editing the Docker env
#      writes straight through into the operator's host .env — which is how a
#      45-key host .env became a 3-key one.
#   2. The stack always runs under an explicit `-p devserver` project name.
#      Without it Compose names the project after the directory (`docker`), and
#      the dev and release files then resolve to the SAME volume names.
#   3. The generated Postgres password is alphanumeric. It is interpolated into
#      a postgresql://user:PASSWORD@host URL, where @ / : silently corrupt it.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
DOCKER_DIR="${PROJECT_ROOT}/docker"
COMPOSE_FILE="${DOCKER_DIR}/docker-compose.release.yml"
ENV_FILE="${DOCKER_DIR}/.env"
PROJECT_NAME="${DEVSERVER_STACK:-devserver}"

YES="${DEVSERVER_SETUP_YES:-0}"

c_reset=$'\033[0m'; c_bold=$'\033[1m'; c_dim=$'\033[2m'
c_green=$'\033[32m'; c_yellow=$'\033[33m'; c_red=$'\033[31m'; c_cyan=$'\033[36m'

hd()   { printf '\n%s%s%s\n' "$c_bold" "$*" "$c_reset"; }
ok()   { printf '  %s✓%s %s\n' "$c_green" "$c_reset" "$*"; }
warn() { printf '  %s!%s %s\n' "$c_yellow" "$c_reset" "$*"; }
err()  { printf '  %s✗%s %s\n' "$c_red" "$c_reset" "$*" >&2; }
note() { printf '    %s%s%s\n' "$c_dim" "$*" "$c_reset"; }

# ask VAR "prompt" "default"  — honours DEVSERVER_SETUP_YES and any preset VAR.
ask() {
    local __var="$1" __prompt="$2" __default="${3:-}" __reply
    local __preset="${!__var:-}"
    if [[ -n "$__preset" ]]; then printf -v "$__var" '%s' "$__preset"; return; fi
    if [[ "$YES" == "1" || ! -t 0 ]]; then printf -v "$__var" '%s' "$__default"; return; fi
    read -rp "  ${__prompt} [${__default}]: " __reply || __reply=""
    printf -v "$__var" '%s' "${__reply:-$__default}"
}

ask_secret() {
    local __var="$1" __prompt="$2" __reply
    local __preset="${!__var:-}"
    if [[ -n "$__preset" ]]; then return; fi
    if [[ "$YES" == "1" || ! -t 0 ]]; then printf -v "$__var" '%s' ""; return; fi
    read -rsp "  ${__prompt} (leave blank to skip): " __reply || __reply=""
    echo
    printf -v "$__var" '%s' "$__reply"
}

port_free() { ! (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

# ── Prerequisites ────────────────────────────────────────────────────────────
hd "DevServer — published image setup"

command -v docker >/dev/null 2>&1 || { err "docker not found — https://docs.docker.com/get-docker/"; exit 1; }
ok "docker $(docker --version | awk '{print $3}' | tr -d ,)"
docker compose version >/dev/null 2>&1 || { err "docker compose plugin not found"; exit 1; }
ok "compose $(docker compose version --short)"
docker info >/dev/null 2>&1 || { err "Docker daemon is not running — start Docker Desktop and re-run."; exit 1; }
ok "daemon reachable"
[[ -f "$COMPOSE_FILE" ]] || { err "missing $COMPOSE_FILE"; exit 1; }

# An existing symlink is the setup-docker.sh hazard described at the top: every
# write below would land in the operator's host .env instead.
if [[ -L "$ENV_FILE" ]]; then
    warn "docker/.env is a symlink to $(readlink "$ENV_FILE")"
    note "Replacing it with a real file so this setup cannot overwrite your host .env."
    rm -f "$ENV_FILE"
fi

# Values a re-run must carry over from the previous docker/.env. Above all the
# bundled database password: POSTGRES_PASSWORD only applies when the volume is
# first initialised, so a freshly generated one would lock the worker out.
PREV_PGPASSWORD=""
PREV_PGHOST_CONTAINER=""
if [[ -f "$ENV_FILE" ]]; then
    warn "docker/.env already exists — backing up before rewriting"
    cp "$ENV_FILE" "${ENV_FILE}.bak-$(date +%Y%m%d-%H%M%S)"
    PREV_PGPASSWORD="$(sed -n 's/^PGPASSWORD=//p' "$ENV_FILE" | tail -n 1)"
    PREV_PGHOST_CONTAINER="$(sed -n 's/^PGHOST_CONTAINER=//p' "$ENV_FILE" | tail -n 1)"
fi

# ── 1. Database ──────────────────────────────────────────────────────────────
hd "1. Database"
echo "  1) Bundled  — run PostgreSQL (pgvector) in a container, managed here"
echo "  2) External — point at a PostgreSQL you already run (host or remote)"
echo
ask DB_CHOICE "Choice" "1"

BUNDLED_DB=1
GENERATED_PW=0
PGHOST_CONTAINER="postgres"; PGPORT_CONTAINER="5432"
PGUSER="devserver"; PGDATABASE="devserver"; PGPASSWORD="${PGPASSWORD:-}"

if [[ "$DB_CHOICE" == "2" ]]; then
    BUNDLED_DB=0
    echo
    note "A database on THIS host is reachable from the containers as"
    note "host.docker.internal (already mapped in the compose file)."
    ask PGHOST_CONTAINER "Postgres host" "host.docker.internal"
    ask PGPORT_CONTAINER "Postgres port" "5432"
    ask PGUSER          "Postgres user" "devserver"
    ask PGDATABASE      "Database name" "devserver"
    ask_secret PGPASSWORD "Postgres password"
    [[ -n "$PGPASSWORD" ]] || { err "an external database needs a password"; exit 1; }
    note "pgvector is required, not optional — semantic memory recall uses it."
else
    if [[ -z "$PGPASSWORD" && -n "$PREV_PGPASSWORD" && "${PREV_PGHOST_CONTAINER:-postgres}" == "postgres" ]]; then
        # Re-run against the same bundled database: keep its password.
        PGPASSWORD="$PREV_PGPASSWORD"
        ok "reusing the bundled database password from the existing docker/.env"
    elif [[ -z "$PGPASSWORD" ]]; then
        # Alphanumeric only: this value goes into a postgresql:// URL.
        PGPASSWORD="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 28)"
        GENERATED_PW=1
        ok "generated a password for the bundled database"
    fi
fi

# A password change cannot reach an already-initialised volume: POSTGRES_PASSWORD
# is read only on first init, so the worker would then die with
# InvalidPasswordError and Compose would report only "dependency failed to start".
# Only a NEW password (generated, or preset and different) can hit this.
if [[ "$BUNDLED_DB" == "1" && ( "$GENERATED_PW" == "1" || "$PGPASSWORD" != "$PREV_PGPASSWORD" ) ]] \
   && docker volume inspect "${PROJECT_NAME}_postgres_data" >/dev/null 2>&1; then
    warn "volume ${PROJECT_NAME}_postgres_data already exists"
    note "Postgres ignores POSTGRES_PASSWORD on an initialised volume, so a NEW"
    note "password here would not apply and the worker would fail to authenticate."
    note "Either keep the old password, or reset it once the stack is up with:"
    note "  docker exec -i ${PROJECT_NAME}-postgres psql -U ${PGUSER} -d ${PGDATABASE} \\"
    note "    -c \"ALTER USER ${PGUSER} PASSWORD '<pw>';\""
fi

# ── 2. Image ─────────────────────────────────────────────────────────────────
hd "2. Image"
ask DEVSERVER_IMAGE "Repository" "dockersergio/devserver"
note "One repository, one tag per tier: worker-* and web-*. Both tags are built"
note "from the same commit, so they are pinned together or not at all."
ask DEVSERVER_BUILD "Build — 'latest' moves, <build>-<sha> is reproducible" "latest"
if [[ "$DEVSERVER_BUILD" == "latest" ]]; then
    DEVSERVER_WORKER_VERSION="worker-latest"
    DEVSERVER_WEB_VERSION="web-latest"
    # A moving pointer must re-resolve every run, or `up` keeps whatever
    # layers this host has cached and "latest" quietly means "last week".
    PULL_POLICY="always"
else
    DEVSERVER_WORKER_VERSION="worker-${DEVSERVER_BUILD}"
    DEVSERVER_WEB_VERSION="web-${DEVSERVER_BUILD}"
    PULL_POLICY="missing"
fi

# ── 3. Ports ─────────────────────────────────────────────────────────────────
hd "3. Ports"
ask WEB_PORT         "Dashboard port" "3200"
ask WORKER_HOST_PORT "Worker API port" "8000"
for p in "$WEB_PORT:dashboard" "$WORKER_HOST_PORT:worker"; do
    port="${p%%:*}"; what="${p##*:}"
    port_free "$port" || warn "port ${port} (${what}) is already in use — 'up' will fail to bind"
done
note "The worker API binds 127.0.0.1 only: its sole auth is INTERNAL_API_TOKEN."

# The bundled DB publishes a host port for psql and the backup/restore pair.
# The containers never use it — they reach the `postgres` service over the
# compose network — so a host that already runs PostgreSQL on 5432 should get
# the published port moved aside, not a bind error that stops the whole stack.
if [[ "$BUNDLED_DB" == "1" ]]; then
    PGPORT="${PGPORT:-5432}"
    if ! port_free "$PGPORT"; then
        taken="$PGPORT"
        for cand in $(seq $((taken + 1)) $((taken + 20))); do
            if port_free "$cand"; then PGPORT="$cand"; break; fi
        done
        if [[ "$PGPORT" == "$taken" ]]; then
            err "port ${taken} is in use and nothing is free in $((taken + 1))-$((taken + 20))"
            exit 1
        fi
        warn "port ${taken} is in use — publishing the bundled database on ${PGPORT} instead"
        note "Containers are unaffected; this is only the host-side port."
    fi
fi

# ── 4. Agent credentials ─────────────────────────────────────────────────────
hd "4. Agent credentials"
note "Claude Max / subscription login on a macOS host lives in the keychain, not"
note "a file, so mounting ~/.claude carries nothing there. API mode with a key is"
note "the portable choice (Linux hosts can mount ~/.claude instead — see the"
note "commented claude_auth volume in docker/docker-compose.release.yml)."
ask_secret ANTHROPIC_API_KEY "Anthropic API key"
if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
    warn "no key — the stack will run, but agent tasks will fail with 'Not logged in'"
fi

# ── 5. First-run behaviour ───────────────────────────────────────────────────
hd "5. First run"
ask INSTANCE_NAME "Instance name (blank = single instance)" ""
ask DEVSERVER_SETUP_COMPLETED "Skip the first-boot setup wizard? (true/false)" "true"

# ── Write docker/.env ────────────────────────────────────────────────────────
hd "Writing ${ENV_FILE#"$PROJECT_ROOT"/}"
umask 077
{
    printf '# DevServer — published-image deployment.\n'
    printf '# Generated by scripts/setup-release.sh on %s.\n' "$(date '+%Y-%m-%d %H:%M:%S')"
    printf '# A REAL FILE on purpose: never symlink this to the repo-root .env.\n\n'
    printf '# Compose project / container-name prefix (docker compose -p).\n'
    printf 'DEVSERVER_STACK=%s\n'             "$PROJECT_NAME"
    printf 'DEVSERVER_IMAGE=%s\n'             "$DEVSERVER_IMAGE"
    printf '# One tag per tier, both built from the same commit — keep them in step.\n'
    printf 'DEVSERVER_WORKER_VERSION=%s\n'    "$DEVSERVER_WORKER_VERSION"
    printf 'DEVSERVER_WEB_VERSION=%s\n'       "$DEVSERVER_WEB_VERSION"
    printf 'DEVSERVER_PULL_POLICY=%s\n\n'     "$PULL_POLICY"
    printf '# Database (%s)\n' "$([[ "$BUNDLED_DB" == 1 ]] && echo bundled || echo external)"
    printf 'PGHOST_CONTAINER=%s\n' "$PGHOST_CONTAINER"
    printf 'PGPORT_CONTAINER=%s\n' "$PGPORT_CONTAINER"
    printf 'PGUSER=%s\n'          "$PGUSER"
    printf 'PGDATABASE=%s\n'      "$PGDATABASE"
    printf 'PGPASSWORD=%s\n\n'    "$PGPASSWORD"
    printf '# Ports\n'
    printf 'WEB_PORT=%s\n'         "$WEB_PORT"
    printf 'WORKER_HOST_PORT=%s\n' "$WORKER_HOST_PORT"
    if [[ "$BUNDLED_DB" == "1" ]]; then
        printf '# Host-side port for the bundled database. Loopback via PGBIND;\n'
        printf '# the containers use the service name and ignore this.\n'
        printf 'PGPORT=%s\n' "$PGPORT"
    fi
    printf '\n'
    printf '# Agent auth — API mode; Max/subscription cannot work in a container.\n'
    printf 'ANTHROPIC_API_KEY=%s\n\n' "${ANTHROPIC_API_KEY:-}"
    printf '# First-run bootstrap (docker/devserver-bootstrap.py)\n'
    printf 'DEVSERVER_SETUP_COMPLETED=%s\n' "$DEVSERVER_SETUP_COMPLETED"
    printf 'INSTANCE_NAME=%s\n' "$INSTANCE_NAME"
    printf '# Seed any setting on an empty database: DEVSERVER_SETTING_<KEY>=value\n'
    printf 'DEVSERVER_SETTING_MODE=autonomous\n'
} > "$ENV_FILE"
chmod 600 "$ENV_FILE"
ok "wrote $ENV_FILE (mode 600, gitignored)"

# ── Pull + start ─────────────────────────────────────────────────────────────
# The profile is appended to COMPOSE rather than kept in its own array: macOS
# ships bash 3.2, where expanding an EMPTY array under `set -u` is a fatal
# "unbound variable" — exactly the external-database path (same as rundockerhub.sh).
COMPOSE=(docker compose -p "$PROJECT_NAME" -f "$COMPOSE_FILE")
PROFILE_NOTE=""
if [[ "$BUNDLED_DB" == "1" ]]; then
    COMPOSE+=(--profile bundled-db)
    PROFILE_NOTE=" --profile bundled-db"
fi

hd "Pulling ${DEVSERVER_WORKER_VERSION} + ${DEVSERVER_WEB_VERSION}"
(cd "$DOCKER_DIR" && "${COMPOSE[@]}" pull -q) || { err "pull failed — private repo? run: docker login. Tag missing? check: docker manifest inspect ${DEVSERVER_IMAGE}:${DEVSERVER_WORKER_VERSION}"; exit 1; }
ok "pulled"

hd "Starting"
(cd "$DOCKER_DIR" && "${COMPOSE[@]}" up -d)

# The worker applies migrations on an empty schema before serving, so healthy
# genuinely means ready here rather than merely listening.
printf '\n  waiting for health'
for _ in $(seq 1 40); do
    if curl -fsS -m 3 "http://127.0.0.1:${WORKER_HOST_PORT}/health" >/dev/null 2>&1; then break; fi
    printf '.'; sleep 3
done
echo

# ── Report ───────────────────────────────────────────────────────────────────
hd "Status"
(cd "$DOCKER_DIR" && "${COMPOSE[@]}" ps --format 'table {{.Name}}\t{{.Status}}')

if curl -fsS -m 5 "http://127.0.0.1:${WORKER_HOST_PORT}/health" >/dev/null 2>&1; then
    ok "worker healthy"
else
    err "worker not healthy — the real cause is in the log, not in compose's"
    err "\"dependency failed to start\" message:"
    note "docker logs ${PROJECT_NAME}-worker"
fi

hd "Done"
printf '  Dashboard   %shttp://localhost:%s%s\n' "$c_cyan" "$WEB_PORT" "$c_reset"
printf '  Worker API  http://127.0.0.1:%s\n' "$WORKER_HOST_PORT"
echo
note "logs:    docker compose -p ${PROJECT_NAME} -f docker/docker-compose.release.yml logs -f"
note "stop:    docker compose -p ${PROJECT_NAME} -f docker/docker-compose.release.yml${PROFILE_NOTE} down"
note "re-run this script any time; it backs up docker/.env first."
