# syntax=docker/dockerfile:1.7
#
# DevServer — ONE Dockerfile, every artifact. Pick one with `--target`.
#
#   uvbin ──┐
#   pydeps ─┴──────────────> worker   (agent tier)
#   webdeps ──> webbuild ──> web      (dashboard tier)
#
# Build from the repo root:
#   docker build -f docker/Dockerfile --target worker -t devserver:worker .
#   docker build -f docker/Dockerfile --target web    -t devserver:web .
#
# TWO IMAGES, ONE TIER EACH. Each target has its own CMD and its own
# HEALTHCHECK, so there is nothing to dispatch at runtime and nothing to keep in
# agreement between a `command:` and a role variable. Compose pins both tiers to
# one ${DEVSERVER_VERSION}, and CI builds them from this file in a single run,
# which is what keeps them on the same version.
#
# This replaced a `combined` target that served both tiers from one tag via an
# entrypoint dispatcher (docker/devserver-entrypoint.sh) and a role-aware probe
# (docker/devserver-healthcheck.sh). One tag did make version skew impossible,
# but it cost two scripts, a stage, a DEVSERVER_ROLE variable that had to match
# `command:` in every compose file, and — the part that actually hurt — a web
# container carrying the Python venv, the agent toolchain and a 219 MB embedding
# model it never touches. The dashboard tier is ~336 MB on its own against
# ~1.9 GB as `combined`.
#
# WHY ONE FILE. This replaced four Dockerfiles describing one product, which
# shared ~100 byte-identical lines (the git identity, uid 10001, the baked
# embedding model, the /app COPY set) so every change was several edits that had
# to agree; and apps/worker/Dockerfile was a single-stage relic (pip, not uv;
# root; no healthcheck) kept only for fast iteration on the Python layer — which
# `--target pydeps` now does properly.

ARG PYTHON_VERSION=3.14-slim
# ONE image variable for the Node build AND the Node runtime, so build and
# runtime can never disagree on libc. The traced standalone bundle carries
# prebuilt native modules (@next/swc, sharp) selected for the BUILD platform, so
# a musl-built bundle segfaults on a glibc runtime and vice versa. Debian costs
# ~70 MB against an alpine pair; moving both to alpine is a real option now that
# nothing shares this stage with the Python tier, but it is a change to make
# deliberately and measure, not a default to drift into.
ARG NODE_BUILD_IMAGE=node:22-bookworm-slim
ARG UV_VERSION=0.9.28

# uv is pinned too: it is the resolver, so its version is part of what makes
# `--locked` reproducible.
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uvbin

# ── Stage: Python dependencies → a self-contained venv ───────────────────────
FROM python:${PYTHON_VERSION} AS pydeps

COPY --from=uvbin /uv /usr/local/bin/uv

# build-essential exists ONLY in this stage. Any dependency without a wheel for
# the target platform is compiled here and the final stages copy the finished
# venv, so no compiler toolchain ships in a released image.
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    VIRTUAL_ENV=/opt/venv

WORKDIR /src

# Dependencies only — the worker's own source is not a package at runtime, it is
# imported off PYTHONPATH (see the worker stage). Layer is keyed on the lock file, so
# source edits never re-resolve. `--locked` makes a pyproject.toml that has
# drifted from uv.lock a loud build failure rather than a silent re-resolution.
COPY apps/worker/pyproject.toml apps/worker/uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

# ── Pre-download the local embedding model ───────────────────────────────────
# Bakes the fastembed weights into the image so the first memory recall runs
# offline-clean. Must match EMBEDDING_MODEL in .env. Placed before the MCP copy
# so churn there does not re-download ~400 MB of ONNX.
ARG EMBEDDING_MODEL=BAAI/bge-base-en-v1.5
ENV EMBEDDING_MODEL=${EMBEDDING_MODEL} \
    FASTEMBED_CACHE_PATH=/opt/fastembed
RUN /opt/venv/bin/python -c \
    "from fastembed import TextEmbedding; TextEmbedding(model_name='${EMBEDDING_MODEL}')"

# DevServer MCP server — the Pro assistant's tool lane, installed into the same
# venv so `devserver-mcp` is on PATH when an agent CLI spawns it. The bracketed
# path makes the COPY optional: `strip-pro.sh` deletes apps/mcp-memory, and the
# free image must still build.
COPY apps/mcp-memor[y] ./mcp-memory/
# PYTHON_SOURCELESS also applies here: the MCP server is Pro, and `uv pip
# install` lands its .py files in site-packages, so compiling /app/src alone
# would still leave readable Pro source in the image.
ARG PYTHON_SOURCELESS=1
COPY --chmod=0755 docker/sourceless.sh /usr/local/bin/sourceless.sh
RUN if [ -f ./mcp-memory/pyproject.toml ]; then \
        uv pip install --python /opt/venv/bin/python --no-cache ./mcp-memory; \
        sourceless.sh "$(/opt/venv/bin/python -c 'import devserver_memory_mcp as m, os; print(os.path.dirname(m.__file__))')"; \
    else \
        echo "apps/mcp-memory absent (free edition) — skipping MCP server"; \
    fi

# ── Stage: web dependencies ──────────────────────────────────────────────────
FROM ${NODE_BUILD_IMAGE} AS webdeps
WORKDIR /app
COPY apps/web/package.json apps/web/package-lock.json* ./
RUN npm ci --prefer-offline --no-audit --fund=false

# ── Stage: web build → .next/standalone ──────────────────────────────────────
# `output: 'standalone'` (apps/web/next.config.ts) emits .next/standalone: the
# app plus only the node_modules the build actually traced — ~60 MB against
# ~640 MB for the full install.
FROM ${NODE_BUILD_IMAGE} AS webbuild
WORKDIR /app
COPY --from=webdeps /app/node_modules ./node_modules
COPY apps/web/ .
# next.config.ts reads `../../version.json` relative to apps/web. In this stage
# apps/web IS /app, so that path resolves to /version.json.
COPY version.json /version.json
ENV DEPLOY_MODE=docker \
    NEXT_TELEMETRY_DISABLED=1
# `npm run build` is two steps: `next build --webpack`, which emits
# .next/standalone, then `tsc -p tsconfig.server.json`, which compiles the
# custom server (server.ts → server.js).
RUN npm run build

# ═════════════════════════════════════════════════════════════════════════════
# Target: worker — the agent tier. FastAPI, the PgQueuer consumer, and the
# runtime installer that fetches whichever vendor CLI a task selects.
# ═════════════════════════════════════════════════════════════════════════════
FROM python:${PYTHON_VERSION} AS worker

# ── System deps ──────────────────────────────────────────────────────────────
# Node runs the npm-distributed agent CLIs the installer fetches at runtime.
# tini reaps zombies — the worker spawns agent subprocesses continuously — and
# with -g forwards SIGTERM past the CMD's tee pipeline.
ARG NODE_MAJOR=22
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates gnupg tini \
    && curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get purge -y --auto-remove gnupg \
    && rm -rf /var/lib/apt/lists/*

# ── Coding-agent CLIs — FETCHED AT RUNTIME, NOT BAKED ────────────────────────
# Each task picks a vendor (anthropic/google/openai/glm) and the worker shells
# out to that vendor's CLI:
#
#   anthropic → claude  (@anthropic-ai/claude-code)      ~316 MB
#   openai    → codex   (@openai/codex)                  ~259 MB
#   google    → agy     (Antigravity CLI, not on npm)    ~196 MB
#   glm       → claude  (the same binary, pointed at Zhipu's Anthropic-compatible
#                        endpoint via ANTHROPIC_BASE_URL/ANTHROPIC_AUTH_TOKEN).
#                        There is no separate GLM launcher to install.
#
# The image installs NONE of them. Baking all three cost 771 MB — 41% of a
# 1.9 GB image — to serve four vendors of which a deployment typically uses one.
# services/agent_cli_installer.py fetches the one a task selects, on first use,
# into AGENT_CLI_DIR below; compose backs that path with a named volume, so the
# download is once per deployment rather than once per container start, and a
# vendor you never select is never fetched at all.
#
# Version pins live in config.py (CLAUDE_CODE_VERSION / CODEX_VERSION /
# AGY_VERSION), so a vendor's breaking release does not reach every deployment
# at once. There is deliberately no build-time bake-back: the manifest parsing
# and SHA-512 verification it needed for agy duplicated the installer's logic in
# shell, and defaulted to off in every build we ship. To pre-provision an
# air-gapped host, populate the agent_clis volume from a machine that has
# network access and set AGENT_CLI_AUTO_INSTALL=false — a missing vendor then
# fails fast with an actionable message instead of reaching for the network.

# ── Git identity ─────────────────────────────────────────────────────────────
# System-level (not --global): --global would write /root/.gitconfig, which the
# unprivileged runtime user never reads. Override via GIT_USER_EMAIL /
# GIT_USER_NAME in your .env — the worker reapplies the identity per worktree.
RUN git config --system user.email "devserver@devserver.local" \
 && git config --system user.name "DevServer Agent" \
 && git config --system init.defaultBranch main \
 && git config --system safe.directory '*'

# ── Unprivileged user ────────────────────────────────────────────────────────
# uid/gid 10001 is shared with the `web` target deliberately: both mount the
# same `worker_logs` named volume, and mismatched owners would leave one of them
# unable to create its log file. A real home directory is required — every agent
# CLI keeps its OAuth/session state under $HOME (~/.claude, ~/.codex,
# ~/.antigravity), which is what "Max (subscription)" billing uses.
RUN groupadd -g 10001 devserver \
 && useradd -u 10001 -g 10001 -m -d /home/devserver -s /bin/bash devserver

# ── Python app ───────────────────────────────────────────────────────────────
COPY --from=pydeps /opt/venv /opt/venv
# fastembed writes lock files beside its weights, so this one is chowned on the
# way in: a later `chown -R` would duplicate every byte of the model into a
# second layer (~220 MB of pure waste).
COPY --from=pydeps --chown=devserver:devserver /opt/fastembed /opt/fastembed

ARG EMBEDDING_MODEL=BAAI/bge-base-en-v1.5
# /opt/agents/bin comes FIRST so a CLI the operator installs or pins into the
# volume shadows anything baked into the image. AGENT_CLI_DIR is the npm
# --prefix the runtime installer writes to; compose backs it with a named
# volume so the download survives container replacement.
ENV PATH="/opt/agents/bin:/opt/venv/bin:${PATH}" \
    AGENT_CLI_DIR=/opt/agents \
    VIRTUAL_ENV=/opt/venv \
    PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    HOME=/home/devserver \
    EMBEDDING_MODEL=${EMBEDDING_MODEL} \
    FASTEMBED_CACHE_PATH=/opt/fastembed \
    NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    WEB_PORT=3200 \
    WORKER_PORT=8000 \
    HOSTNAME=0.0.0.0

WORKDIR /app

COPY --chown=devserver:devserver apps/worker/src/ ./src/
COPY --chown=devserver:devserver .claude/skills/ ./.claude/skills/
# Ask Agent knowledge pack — without it the assistant has no grounding and
# answers about DevServer from memory instead of from the handbook.
COPY --chown=devserver:devserver docs/assistant/ ./docs/assistant/
# Maintenance scripts the Ask Agent panel can propose running (behind an
# explicit operator click). Note that inside a container `dbrestart.sh` and
# `restart.sh` cannot reach host services — the action runner detects the
# container and tells the operator to run them on the host instead.
COPY --chown=devserver:devserver scripts/ ./scripts/

# The schema itself. scripts/migrate.sh has always been in the image but the
# SQL it reads was not, and it shells out to psql, which is not installed here
# — so a fresh Postgres volume came up with an EMPTY schema while the worker
# still reported healthy. devserver-bootstrap.py applies these over asyncpg on
# first start instead. Keep the path in step with DEVSERVER_MIGRATIONS_DIR.
COPY --chown=devserver:devserver database/migrations/ ./database/migrations/

# ── Production build: bytecode only, no Python source ────────────────────────
# See docker/sourceless.sh for how and why. Deleting the sources is what makes
# the Pro modules (services/pro/**, routes/pro_internal.py) ship as binaries.
#
# Pro is NOT stripped from this image — it is compiled in and gated at runtime
# by services/pro/__init__.py's _LicenseGatedHooks, which routes every call to
# the real implementation when licensing.is_pro_active() and to the free no-ops
# otherwise. So an unlicensed deployment of this image behaves exactly like the
# free edition, and a licensed one unlocks Pro without pulling a second image.
ARG PYTHON_SOURCELESS=1
COPY --chmod=0755 docker/sourceless.sh /usr/local/bin/sourceless.sh
RUN sourceless.sh /app/src && chown -R devserver:devserver /app/src

COPY --chmod=0755 docker/devserver-bootstrap.py /usr/local/bin/devserver-bootstrap.py

# Runtime dirs, pre-created and owned by the runtime user so a fresh named
# volume inherits that ownership instead of root's. /opt/agents is where the
# vendor CLIs land on first use; it MUST exist and be devserver-owned here,
# because Docker seeds a new named volume from the image's directory — an
# absent or root-owned path leaves the unprivileged worker unable to install.
# /app/config holds the writable .env the Settings/Setup Database card edits.
# It cannot live at /app/.env: WORKDIR /app is root-owned 755, so the worker's
# own save died with PermissionError on it (and, before DEVSERVER_ENV_FILE, on
# "/" — three levels up from the flattened /app/src is container root). Same
# named-volume ownership rule as the paths above: devserver-owned here, so the
# volume seeded from it is writable and the file survives a container recreate.
RUN mkdir -p /app/worktrees/.bare /app/logs/tasks /app/config /opt/agents/bin \
 && chown -R devserver:devserver /app/worktrees /app/logs /app/config /opt/agents

USER devserver

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=5 \
  CMD curl -fsS "http://127.0.0.1:${WORKER_PORT:-8000}/health" -o /dev/null || exit 1

# tini -g reaps the agent subprocesses the worker spawns continuously, and
# forwards SIGTERM past the tee pipeline below — without it `docker stop` would
# never reach uvicorn's shutdown handler.
ENTRYPOINT ["/usr/bin/tini", "-g", "--"]

# First-start bootstrap applies the schema to an empty database and seeds
# settings from the environment; only this tier runs it, so nothing races it for
# the same migrations. It is never fatal — the script swallows its own errors and
# exits 0, because a bootstrap problem should surface in the log rather than as a
# tier that will not boot.
#
# Output is tee'd into the shared worker_logs volume so the in-app /logs viewer
# sees it. Best-effort: an unwritable log dir costs the viewer, it must not take
# the tier down, and the warning names the fix.
CMD ["sh", "-c", "mkdir -p /app/logs 2>/dev/null; /opt/venv/bin/python /usr/local/bin/devserver-bootstrap.py 2>&1 | tee -a /app/logs/worker.log || true; if touch /app/logs/worker.log 2>/dev/null; then uvicorn src.main:app --host 0.0.0.0 --port ${WORKER_PORT:-8000} 2>&1 | tee -a /app/logs/worker.log; else echo \"[warn] /app/logs is not writable by uid $(id -u) — worker.log disabled, so the in-app /logs viewer will not show this container. Fix: chown -R 10001:10001 on the log volume/dir.\"; exec uvicorn src.main:app --host 0.0.0.0 --port ${WORKER_PORT:-8000}; fi"]

# OCI labels last on purpose: VERSION changes every build, and any layer below
# an ARG that consumes it is invalidated. Keeping them at the very end of the
# stage means a version bump rebuilds nothing but this metadata layer.
ARG VERSION=dev
ARG GIT_SHA=unknown
LABEL org.opencontainers.image.title="DevServer Worker" \
      org.opencontainers.image.description="DevServer agent worker: FastAPI, PgQueuer consumer, and the runtime installer for the vendor coding-agent CLIs" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.source="https://github.com/sergiovision/DevServer" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.vendor="capitaltools.app"

# ── Target: web — the dashboard tier ─────────────────────────────────────────
# No Python, no agent CLIs, no embedding model: ~336 MB. Every topology runs
# this one for the dashboard, including "worker on host", which needs no agent
# tier in Docker at all.
FROM ${NODE_BUILD_IMAGE} AS web

# tini reaps zombies and, with -g, forwards SIGTERM to the whole process group.
# The CMD below is a pipeline, so node is not PID 1; without this `docker stop`
# would never reach the server's SIGTERM handler and would always hard-kill it
# after the grace period. curl is for the healthcheck — the slim Node image is
# not guaranteed to carry a fetcher.
RUN apt-get update && apt-get install -y --no-install-recommends tini curl \
 && rm -rf /var/lib/apt/lists/*

# uid/gid 10001 matches the worker stage deliberately: both containers mount the same
# `worker_logs` named volume, and mismatched owners would leave one of them
# unable to create its log file. No home dir — this tier spawns no agent CLI.
RUN groupadd -g 10001 devserver \
 && useradd -u 10001 -g 10001 --no-create-home -d /app -s /usr/sbin/nologin devserver

WORKDIR /app

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    WEB_PORT=3200 \
    HOSTNAME=0.0.0.0

# The traced app and its node_modules. This also lays down Next's own generated
# `server.js` entrypoint, which the fourth COPY deliberately replaces.
COPY --from=webbuild --chown=devserver:devserver /app/.next/standalone ./
COPY --from=webbuild --chown=devserver:devserver /app/.next/static ./.next/static
COPY --from=webbuild --chown=devserver:devserver /app/public ./public
COPY --from=webbuild --chown=devserver:devserver /app/server.js ./server.js

# Mount point for the log volume shared with the worker container, pre-created
# so a fresh named volume inherits the unprivileged owner rather than root.
RUN mkdir -p /app/logs && chown devserver:devserver /app/logs

USER devserver

EXPOSE 3200

# /setup is the one route the first-boot middleware never intercepts and that
# renders without a database, so it answers 200 in both configured and
# unconfigured states.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${WEB_PORT}/setup" -o /dev/null || exit 1

ENTRYPOINT ["/usr/bin/tini", "-g", "--"]
# Output is tee'd into /app/logs/web.log so the in-app /logs viewer (which
# tails the file) works inside docker too. The worker_logs volume is shared
# with the worker container so both logs are visible from one place.
#
# The write is best-effort: a log dir that predates the unprivileged uid (or a
# bind-mounted host dir owned by someone else) is a reason to lose the /logs
# viewer, not a reason to take the whole web tier down. The warning names the
# fix rather than failing silently.
CMD ["sh", "-c", "mkdir -p /app/logs 2>/dev/null; if touch /app/logs/web.log 2>/dev/null; then node server.js 2>&1 | tee -a /app/logs/web.log; else echo \"[warn] /app/logs is not writable by uid $(id -u) — web.log disabled, so the in-app /logs viewer will not show this container. Fix: chown -R 10001:10001 on the log volume/dir.\"; exec node server.js; fi"]

ARG VERSION=dev
ARG GIT_SHA=unknown
LABEL org.opencontainers.image.title="DevServer Web" \
      org.opencontainers.image.description="DevServer dashboard, task API, and live-update WebSocket" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.source="https://github.com/sergiovision/DevServer" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.vendor="capitaltools.app"
