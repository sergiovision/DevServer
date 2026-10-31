# Quick Start

Everything needed to install, run and upgrade DevServer on macOS, Linux and Windows — host processes or Docker. Back to the [README](../README.md).

## Prerequisites

- Node.js >= 22 LTS
- Python >= 3.14
- PostgreSQL >= 16
- At least one agent CLI installed and authenticated (e.g. `claude login`)
- `uv` for Python dependency management — [install guide](https://docs.astral.sh/uv/)
- A Gitea (or Forgejo) instance with a personal access token — or skip the git host entirely and use a **Local Git** repo (any local clone, no token needed)

## Local setup (host processes)

```bash
git clone https://github.com/<YOUR_GITHUB_HANDLE>/DevServer.git
cd DevServer
cp config/.env.example .env
# edit .env — fill in PGPASSWORD, GITEA_TOKEN, TELEGRAM_*, ANTHROPIC_API_KEY

./scripts/migrate.sh          # runs all SQL migrations
./scripts/start.sh --dev      # starts worker + web in dev mode
```

The dashboard is now at **http://localhost:3200** (configurable via `WEB_PORT` in `.env`).

> **On Windows?** Use the PowerShell scripts (`scripts\setup-local.ps1`,
> `scripts\start.ps1`) instead of the `.sh` ones — see [Windows](#windows).

## Upgrading an existing install

The whole schema lives in one idempotent migration, so upgrading an
existing database is just:

```bash
./scripts/migrate.sh   # safe to re-run; only applies what's missing
```

This adds the latest memory tables/columns and reshapes the
`agent_memory.embedding` column to the local-embedding dimension (768).
Backfill embeddings with the local model afterwards (the reshape drops the
old vectors):

```bash
cd apps/worker && uv run python scripts/reembed_memory.py
```

`fastembed` is a worker dependency; the embedding model downloads on first
use (the Docker image pre-bakes it). No embedding API key is required.

## Docker (recommended for production)

Use the lifecycle scripts — they read the repo-root `.env`, select the right
compose file/topology, and enable the bundled-database profile for you:

```bash
cp config/.env.example .env
# edit .env — minimum: PGPASSWORD, ANTHROPIC_API_KEY

./scripts/build.sh --docker
./scripts/start.sh --docker
```

The bundled `pgvector/pgvector:pg17` database runs under a compose **profile**
(`bundled-db`) so it can be skipped for Host-OS / External database modes. If
you invoke `docker compose` directly instead of the scripts, you must therefore
(a) pass `--profile bundled-db` to start the bundled DB, and (b) make the
repo-root `.env` visible to compose (the scripts `source` it; raw compose reads
only a `.env` in the current dir), e.g.:

```bash
cd docker
set -a && . ../.env && set +a            # export root .env for ${VAR} interpolation
docker compose --profile bundled-db up -d --build
```

This runs out of the box on Linux, macOS, and Windows.

**Two topologies — where do the agent CLIs run?** The worker shells out to a
vendor CLI (`claude` / `agy` / `codex` — GLM also uses `claude`) for every
task, and *where the worker runs* decides where those CLIs run:

| Topology | Compose file | Agent CLIs run… | Auth |
|---|---|---|---|
| **All-in-Docker** (default) | `docker-compose.yml` | inside the worker container (baked into the image) | API keys via `.env`; for `max` subscription mode, mount your host `~/.claude` (see below) |
| **Worker on host** | `docker-compose.host-worker.yml` | as host processes | your existing host logins (`claude login`, `codex login`, `agy` Google OAuth, GLM key) — nothing to install or mount |

Pick **worker-on-host** if you want the agents to use the subscription logins
already set up on your machine; pick **all-in-Docker** for a single portable
stack. Both are documented below.

**One image per tier.** The `worker` and `web` containers run separate tags —
`devserver:worker-latest` and `devserver:web-latest` — built from the one
`docker/Dockerfile` with `--target worker` / `--target web`. Each image carries
its own start command and health probe, so nothing picks a role at runtime.
Keep the pair on one version — build them together from a single commit.

**You build these images yourself.** DevServer publishes nothing to a container
registry, so the tags above are plain local names and there is no `docker
compose pull` path. Set `DEVSERVER_IMAGE` in `.env` if you want to tag and push
them to a registry of your own. Both services declare `image:` *and* `build:`,
which means:

```bash
docker compose up -d --build      # always rebuild (what the scripts do)
docker compose up -d              # build only if the image is absent locally
docker compose up -d --no-build   # run whatever is already tagged locally
```

Everything is built from one `docker/Dockerfile`; pick an artifact with
`--target`:

| Target | What | Used by |
|---|---|---|
| `web` | dashboard only, no Python/agent CLIs (~336 MB) | every topology, including `docker-compose.host-worker.yml` |
| `worker` | agent tier only | `docker-compose.yml` |
| `pydeps` | Python dependency stage, for iterating on the worker's deps | nothing — a build-time convenience |

### Linux / macOS — host PostgreSQL (recommended)

If your host already runs PostgreSQL on port 5432, skip the bundled DB
with the host-DB override:

```bash
# If you previously ran the bundled-DB stack, tear it down first
docker compose down

docker compose -f docker-compose.yml -f docker-compose.host-db.yml up -d --build
```

On Docker Desktop (macOS) `host.docker.internal` is built in; on Linux
it is mapped through the compose override via `host-gateway`.

**Linux host Postgres prerequisites** (one-time):

1. `postgresql.conf` — add the docker bridge gateway to `listen_addresses`
   (check yours with `ip addr show docker0`, default is `172.17.0.1`):
   ```
   listen_addresses = 'localhost,172.17.0.1'
   ```
2. `pg_hba.conf` — allow the docker bridge subnet:
   ```
   host    devserver    devserver    172.16.0.0/12    scram-sha-256
   ```
3. `sudo systemctl reload postgresql` (listen_addresses needs a full
   restart: `sudo systemctl restart postgresql`).

**macOS host Postgres prerequisites** (one-time, Homebrew install):

1. `postgresql.conf` (usually `/opt/homebrew/var/postgresql@17/postgresql.conf`):
   ```
   listen_addresses = '*'
   ```
2. `pg_hba.conf` — allow the Docker Desktop VM subnet:
   ```
   host    devserver    devserver    192.168.65.0/24    scram-sha-256
   host    devserver    devserver    127.0.0.1/32       scram-sha-256
   ```
3. `brew services restart postgresql@17`.

Make sure the `vector` extension is installed on the host Postgres — on
macOS: `brew install pgvector` then `CREATE EXTENSION IF NOT EXISTS vector;`
in the `devserver` database.

### Worker on host (run AI providers from your host OS)

Use this when you want the agents to run on your machine with the
subscription logins you already set up (`claude login`, `codex login`, the
`agy` Google OAuth, GLM key) instead of in a container. Postgres + the web
dashboard stay in Docker; only the Python worker runs natively.

```bash
# 1. DB + web in Docker (no worker container)
cd docker
docker compose -f docker-compose.host-worker.yml up -d --build

# 2. Worker on the host (new terminal, repo root)
cd apps/worker
cp ../../config/.env.example .env     # already host-shaped — then edit it
uv run uvicorn src.main:app --host 0.0.0.0 --port 8000
```

How the boundary is bridged (handled by the compose file):

- **web → worker**: the containerised dashboard reaches the host worker at
  `host.docker.internal:8000` (`WORKER_URL`). Built in on Docker Desktop;
  mapped via `host-gateway` for Linux.
- **worker → DB**: the DB port is published, so the host worker connects on
  `localhost:5432`.
- **logs**: web bind-mounts `<repo>/logs`, so the dashboard `/logs` viewer
  reads the host worker's log files.

Two `.env` values must match this layout:

- `DATABASE_URL` must point at `127.0.0.1:5432` (the published DB port) — the
  `postgres` service name only resolves *inside* Docker.
- `DEVSERVER_ROOT` must be the repo root so `LOG_DIR=${DEVSERVER_ROOT}/logs/tasks`
  lines up with the log dir web mounts.

In this mode the worker is not root, so the Claude/GLM
`--dangerously-skip-permissions` root restriction never triggers, and no agent
CLIs are installed into any image.

**Lifecycle scripts default to this topology.** `./scripts/build.sh --docker`,
`start.sh --docker`, `restart.sh --docker`, and `stop.sh --docker` all use
`docker-compose.host-worker.yml` and manage the host worker for you (build its
venv, start/stop the process). To run the all-in-Docker stack via the scripts
instead, set `DEVSERVER_COMPOSE=all-in-docker`:

```bash
DEVSERVER_COMPOSE=all-in-docker ./scripts/start.sh --docker
```

**Choosing the database (Settings → Database, or the Setup wizard).** In a
Docker deployment the Database card offers three options — **Docker PostgreSQL
(bundled)**, **Host OS PostgreSQL**, or **External host / credentials** (a
host build offers just *local default* vs *custom host/creds*). The card writes
the connection to `.env` and, for the non-bundled options, sets
`DEVSERVER_HOST_DB=1` plus the container-perspective `PGHOST_CONTAINER` /
`PGPORT_CONTAINER` so the web (and, in all-in-Docker, worker) **containers**
reach the right database — not just the host worker. The bundled `postgres`
container lives behind a `bundled-db` compose profile, so picking Host-OS /
External skips it entirely. A worker/web restart applies the change.

### Max subscription (Claude OAuth) inside Docker

For the **all-in-Docker** topology, `max` (subscription) mode needs the OAuth
login from `claude login` — which lives in your host home dir. Run
`claude login` on the host once, then mount the config dir read-only by
uncommenting this line under the `worker` service in `docker-compose.yml`:

```yaml
    # - ${CLAUDE_CONFIG_DIR:-${HOME}/.claude}:/home/devserver/.claude:ro
```

The container runs as the unprivileged `devserver` user (uid 10001), so the
mount target is `/home/devserver/.claude` — the CLI reads `$HOME`.

Leave `ANTHROPIC_API_KEY` empty in `.env` so the CLI falls back to the mounted
login. (The worker-on-host topology above needs none of this — it uses your
host login directly.)

> **Windows:** `${HOME}` is not set for `docker compose` on native Windows, so
> the default above resolves to an invalid path. Set `CLAUDE_CONFIG_DIR` in
> `.env` to your host config dir using forward slashes —
> `CLAUDE_CONFIG_DIR=C:/Users/<you>/.claude` — before uncommenting the mount.

## Windows

Windows is a first-class target. Every `scripts/*.sh` lifecycle script has a
PowerShell twin (`scripts/*.ps1`), so you can run DevServer **natively** (host
processes, no WSL or Docker required) or under **Docker Desktop** — your pick.
Use **PowerShell** (not CMD) for everything below.

> If a script is blocked by the execution policy, allow it for the current
> session only: `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`.

### Option A — Native (PowerShell, recommended for development)

Prerequisites: **Node.js 22+**, **Python 3.14+**, **PostgreSQL 16+**, **uv**
([install](https://docs.astral.sh/uv/)), and at least one agent CLI
(e.g. `npm install -g @anthropic-ai/claude-code` then `claude login`).

```powershell
git clone https://github.com/<YOUR_GITHUB_HANDLE>/DevServer.git
cd DevServer

.\scripts\setup-local.ps1     # 1st run: copies config\.env.example -> .env, then stops
notepad .env                  #          fill in PGPASSWORD and ANTHROPIC_API_KEY (minimum)
.\scripts\setup-local.ps1     # 2nd run: bootstraps the DB role+db, runs migrations, installs deps

.\scripts\start.ps1           # start worker + web (dev mode, hot reload)
```

Open **http://localhost:3200**. `setup-local.ps1` creates the `devserver`
PostgreSQL role and database for you on a fresh host — if your superuser isn't
passwordless, set `PGSUPERUSER` / `PGSUPERPASSWORD` in `.env` or enter the
superuser password when prompted.

Day-to-day commands (all accept `-Dev` (default), `-Prod`, or `-Docker`):

```powershell
.\scripts\start.ps1 -Prod     # production build (next build + node server.js)
.\scripts\stop.ps1            # stop host processes and free the ports
.\scripts\restart.ps1 -Prod   # tear everything down and restart cleanly in a mode
.\scripts\build.ps1 -Prod     # build only (no start)
```

Logs stream to `logs\worker.log` and `logs\web.log`
(`Get-Content logs\worker.log -Wait` to tail).

### Option B — Docker Desktop

1. Install [Docker Desktop for Windows](https://www.docker.com/products/docker-desktop/)
   with the WSL 2 backend, and clone the repo to a path with no spaces.
2. Create and edit `.env` at the repo root:
   ```powershell
   Copy-Item config\.env.example .env
   notepad .env     # set PGPASSWORD and ANTHROPIC_API_KEY at minimum
   ```
3. Build and start with the lifecycle scripts (they pick the compose
   files/profiles and manage the host worker for you):
   ```powershell
   .\scripts\start.ps1 -Docker
   ```
4. Open **http://localhost:3200**. Stop with `.\scripts\stop.ps1 -Docker`.

**Claude Max (subscription) mode on Windows.** To use `max` billing instead of
an API key, run `claude login` on the host once. In **native** mode it just
works (the CLI uses your host login). In **Docker** mode set `CLAUDE_CONFIG_DIR`
in `.env` to your host config dir with forward slashes
(`CLAUDE_CONFIG_DIR=C:/Users/<you>/.claude`) and uncomment the `claude_auth`
mount in `docker-compose.yml` — the `${HOME}/.claude` default that works on
macOS/Linux does **not** resolve for `docker compose` on native Windows, so
`CLAUDE_CONFIG_DIR` is required there.

See also: [Backup & Restore](backup-restore.md).
