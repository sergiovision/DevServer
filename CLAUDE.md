# DevServer

Autonomous development pipeline: dispatches coding tasks to vendor coding-agent
CLIs (Claude Code, Codex, Antigravity, GLM via Claude Code), verifies the
result, and delivers it as a PR (Gitea / GitHub), a patch set, or local
changes. Open source, single edition — every feature ships to everyone; there
is no licensing, no feature gating and no `pro/` split.

Deeper reading, kept current alongside the code:
[`docs/architecture.md`](docs/architecture.md) (how a task becomes a PR, the
runner package, concurrency model, test strategy),
[`docs/design-decisions.md`](docs/design-decisions.md) (why it is shaped that
way), [`docs/features.md`](docs/features.md),
[`docs/quick-start.md`](docs/quick-start.md),
[`docs/backup-restore.md`](docs/backup-restore.md).

## Tech Stack
- **Frontend**: Next.js 16 + React 19 + CoreUI (`apps/web/`), custom `server.ts` (WebSocket relay at `/api/ws`)
- **Worker**: Python 3.14 + FastAPI (`apps/worker/`), managed with `uv`
- **Job queue**: PgQueuer — producer is the Next.js app (SQL insert), consumer is the worker
- **Database**: PostgreSQL + pgvector (bundled container: `pgvector/pgvector:pg17`)
- **Scheduler**: asyncio jobs inside the worker (`services/scheduler.py`)
- **Git**: Gitea / GitHub APIs for PRs, git worktrees for isolation, or a local folder
- **Notifications**: Telegram, Discord, A2A push (`services/notify/`)
- **Embeddings**: local `fastembed` (ONNX/CPU, no API key)

## Directory Layout
```
apps/web/            Next.js dashboard + REST API (src/app/api/*) + queue producer + WS relay
apps/worker/         FastAPI worker + PgQueuer consumer
  src/routes/        HTTP routers, all under /internal (plus /health, /ready)
  src/services/      flat service modules (agent_backends, git_ops, memory, …)
  src/services/runner/        the task runner, split by phase (see architecture.md §3)
  src/services/notify/        notification dispatcher + channel backends
  src/services/import_sources/ external task import adapters (Confluence)
  src/models/        SQLAlchemy models
  tests/             pytest: unit/ (pure) + runner/ (real Postgres + git)
  scripts/           one-off maintenance (reembed_memory.py)
apps/mcp-memory/     stdio MCP server: task-control lane for an interactive CLI
database/migrations/ 001_initial.sql, 002_external_import.sql, 003_a2a.sql,
                     004_post_completion_action.sql (all idempotent)
config/.env.example  env template — copy to repo-root .env
docker/              Dockerfile, compose files, first-start bootstrap
scripts/             lifecycle, setup, migration, backup, service scripts,
                     feature-coverage.mjs (docs/feature-catalogue.md ↔ test tags)
skills/, .claude/skills/  devplan / devtask skills
```

There is no `pro/` directory anywhere. New code goes straight into the flat
module it belongs to (`apps/worker/src/services/<name>.py`,
`apps/web/src/app/api/<name>/`, `apps/web/src/components/<Name>.tsx`).

## Running Locally
```bash
cp config/.env.example .env           # fill in credentials
./scripts/setup-local.sh              # first time: venv, npm ci, DB
./scripts/migrate.sh                  # apply database/migrations
./scripts/start.sh                    # dev (hot reload); --prod builds + node server.js
./scripts/stop.sh | ./scripts/restart.sh
```

By hand:
```bash
cd apps/worker && uv run uvicorn src.main:app --host 0.0.0.0 --port 8000
cd apps/web && npm run dev            # http://localhost:3200
```

## Tests, lint, type-check (what CI runs — `.github/workflows/ci.yml`)
```bash
# Worker
cd apps/worker
uv sync --extra test
uv run ruff check . && uv run ruff format --check .   # blocking
uv run pyright                                         # basic mode, reported only
uv run pytest -q          # tests/runner needs PostgreSQL: DEVSERVER_TEST_DATABASE_URL
                          # (default postgresql:///devserver_test); skipped if unavailable

# Web
cd apps/web
npm ci
npm test                  # node test runner over src/**/*.test.ts
npx tsc --noEmit
npm run build
```

Run the relevant suites before declaring a change done. For `docker/` changes
also run `bash scripts/compose-check.sh`.

## Running with Docker
```bash
cd docker && docker compose up -d --build     # or: ./scripts/start.sh --docker
```

### Image layout — one Dockerfile, one tag per tier

`docker/Dockerfile` is the only Dockerfile; every artifact is a `--target`:

```
uvbin ──┐
pydeps ─┴──────────────> worker   (agent tier, :8000)
webdeps ──> webbuild ──> web      (dashboard tier, :3200)
```

- Each target bakes in its own `CMD` and `HEALTHCHECK`; there is no runtime role
  dispatch and nothing to pass as `command:`.
- Both run as uid/gid 10001 and share the `worker_logs` volume.
- The worker's first-start bootstrap (`docker/devserver-bootstrap.py`) applies
  the migrations to an empty database and seeds `DEVSERVER_SETTING_<KEY>`
  values, so an image works without the repo's migrations bind-mount.
- `NODE_BUILD_IMAGE` (glibc, `node:22-bookworm-slim`) is shared by the web
  build and the web runtime so native modules match the runtime libc.
- **Images are built locally.** `docker-compose.yml` tags
  `devserver:worker-latest` / `devserver:web-latest`; nothing is pulled unless
  you opt into the release path below.

### Agent CLIs are fetched at runtime, not baked

The image ships no vendor CLIs. `services/agent_cli_installer.py`
(`ensure_cli`) fetches the binary a task selects on first use into
`/opt/agents` (the `agent_clis` volume). Keyed on the binary (`anthropic` and
`glm` share `claude`), serialised per binary, never installs over a custom
`CLAUDE_BIN` / `CODEX_BIN` / `GEMINI_BIN`, and a first failure is sticky per
process. Version pins live in `config.py` (`CLAUDE_CODE_VERSION`,
`CODEX_VERSION`, `AGY_VERSION`). Air-gapped hosts pre-populate the volume and
set `AGENT_CLI_AUTO_INSTALL=false`. Reusing host CLIs via bind-mount is
Linux-only (they are native executables).

### Compose files (`docker/`)

| File | Role |
|---|---|
| `docker-compose.yml` | Default stack. `image:` + `build: {target}` per tier; bind-mounts `../database/migrations`. |
| `docker-compose.host-worker.yml` | Self-contained: Postgres + `web` in Docker, worker on the host. Default topology for `scripts/start.sh --docker` (`DEVSERVER_COMPOSE=all-in-docker` for the full stack). |
| `docker-compose.host-db.yml`, `docker-compose.local.yml` | Overrides of the base. |
| `docker-compose.hardened.yml` | Overlay: resource limits, log rotation, `no-new-privileges`, volume backup labels. |
| `docker-compose.release.yml` | Self-contained, `image:` only (no `build:`), for hosts that pull prebuilt images (`DEVSERVER_IMAGE`). Used by `scripts/setup-release.sh` / `scripts/rundockerhub.sh`. |

Shared facts live in the `x-devserver-common` / `x-db-env` YAML anchors. The
bundled Postgres sits behind the `bundled-db` profile. `container_name` is
`${DEVSERVER_STACK:-devserver}-{postgres,worker,web}` and several scripts
`docker exec` those names.

**Overlay order matters**: `host-db.yml` removes Postgres with
`postgres: !reset null` while `hardened.yml` re-declares it, so `host-db.yml`
must come **last**.

`bash scripts/compose-check.sh` validates every legal `-f` combination
(client-side, no daemon) and asserts env-key/image parity between
`docker-compose.yml` and `docker-compose.release.yml` — keep the two in sync.

## Scripts (`scripts/`)

| Script | Purpose |
|---|---|
| `_lib.sh` / `_lib.ps1` | Shared helpers (paths, `.env` loading, port/process helpers, compose file + profile selection). Source it, don't duplicate it. |
| `setup-local.sh`, `setup-docker.sh` | First-time setup (host / build-from-checkout Docker). |
| `start.sh`, `stop.sh`, `restart.sh` (+ `.ps1`) | Lifecycle; `--dev` (default), `--prod`, `--docker`. |
| `migrate.sh`, `dbrestart.sh` | Apply migrations; restart the DB. |
| `build.sh`, `bump-version.sh` | Build for dev / prod / docker; bump the version. |
| `devserver-backup.sh`, `devserver-restore.sh` | `[local\|docker]` DB dump + settings + `.env` archive / restore. |
| `service-install.sh`, `service-run.sh`, `service-uninstall.sh` | macOS launchd LaunchAgent (`devserver.service`) running worker + web as a supervised pair. |
| `setup-release.sh`, `rundockerhub.sh` | Prebuilt-image deployment via `docker-compose.release.yml` (writes a real `docker/.env`, pins `-p devserver`). |
| `compose-check.sh` | Compose parse/drift check. |

## Conventions
- Task statuses: pending → queued → running → verifying → test/done/failed/blocked/cancelled.
  On success the runner writes **`test`** (awaiting human check), never `done`.
- Priority: 1=critical, 2=high, 3=medium, 4=low
- Branch naming: `agent/{task_key}`
- Git identity: `GIT_USER_EMAIL` / `GIT_USER_NAME` in `.env`
- All secrets in `.env` (gitignored, as are `docker/.env` and every `.env.*`
  except `config/.env.example`)
- Real-time updates: `task_events` row → PG `NOTIFY` → `server.ts` → WebSocket → browser
- **Enqueue invariant**: the only PgQueuer producer is
  `apps/web/src/lib/queue.ts::enqueueTask`. The worker never writes to
  `pgqueuer`; it calls back via `services/queue_bridge.enqueue_via_web(task_id)`
  (`POST /api/tasks/{id}/enqueue`), so worker-initiated enqueues need the web
  tier running.
- **The worker has no general authentication.** Everything under `/internal`
  is trusted; only the Ask Agent action path checks `INTERNAL_API_TOKEN`
  (`X-Internal-Token`). Never expose port 8000 — compose binds it to
  `127.0.0.1` by default. External callers go through the Next.js tier
  (A2A gateway, webhooks).
- Settings: key/value `settings` table, read via `services/app_settings.py`
  (`get_setting` / `get_int_setting` / `get_bool_setting`). New behaviours are
  opt-in with defaults that keep an unconfigured deployment unchanged.
- Python: `ruff` clean, sessions opened per unit of work (never across a
  subprocess or a sleep), subprocesses through `services/proc.py` with a
  timeout, git through `git_ops._run` (required `timeout`).

## Task types

`tasks.task_type` (default `coding`, immutable after creation). The web
registry `apps/web/src/lib/task-types.ts` is the single source of truth for
labels and per-type field visibility; the worker decides how each runs.

| Type | Repo | Runner | Output |
|---|---|---|---|
| `coding` | required | heavy: worktree → agent → verifier → preflight → PR/patch | PR / patches |
| `test` | required | heavy, worker verifier bypassed (the agent runs the tests) | results → Task Log |
| `script` | required | heavy, worker verifier bypassed (the agent runs the script) | output → Task Log |
| `skill` | optional | light: `skill_runner.run_lightweight_task`, no commit/PR | `RESULT.md` + Task Log |
| `research` | optional | light; the description IS the prompt | answer → Task Log |

`PATCH /api/tasks/[id]` deliberately omits `task_type`. Templates carry a
`task_type`. A light task tied to a repo runs inside that repo (local folder or
a clean worktree) under the repo lock but never commits or pushes.

## Git host providers

`repos.provider`: `gitea` (default) | `github` | `local`. All in
`services/git_ops.py`:

- `_detect_provider()` re-sniffs the clone-URL host at runtime; an explicit
  non-default column value wins.
- `_auth_url()`: Gitea `https://token:<pat>@host`; GitHub
  `https://x-access-token:<pat>@github.com` (the only form that works for
  classic, fine-grained and App tokens).
- `_resolve_token()`: per-repo `repos.gitea_token` wins; the global fallback is
  provider-specific (`GITEA_TOKEN` / `GITHUB_TOKEN`). A Gitea token is never
  used for a GitHub repo.
- `create_gitea_pr()` (name kept for back-compat) calls the Gitea or GitHub
  (incl. Enterprise `/api/v3`) pulls API.

**Local repos (`provider='local'`)** — the Local Root Folder is stored in
`repos.gitea_url`. `setup_local_repo()` validates the folder; no clone, no
worktree copy. Hard rules: **never push**, **never reset/clean** the
operator's folder. Only two git flows are valid: `patch` (clean tree required;
agent commits on a local `agent/{task_key}` branch, patches are generated in
place, the original branch is restored) and `untracked` (agent only edits
files — no branch, no commit). Agent commits pass `-c user.email/-c user.name`
inline so the operator's repo config is never touched.

## The task runner

`agent_runner.run_task` → `services/runner/` (`phases.prepare_workspace` →
`gather_evidence` → `run_all_attempts` → `finalise`). Every attempt returns a
`Verdict` (`runner/results.py`); see `docs/architecture.md` §2–§4 before
changing control flow.

Pre-run evidence (`runner/evidence.py`, each source optional, failures
degrade):

| Block | Service | Event |
|---|---|---|
| Repo map (symbol index, budget-truncated, `repo_map.DEFAULT_MAX_CHARS`) | `services/repo_map.py` | `repo_map_built` |
| Reality signal (0–100 from repo-map hits, recent commits, open-PR collision, past outcomes) | `services/reality_gate.py` | `reality_signal` |
| Strict abstain gate (opt-in `reality_abstain_threshold`) | `runner/evidence.py` | `reality_abstain` |
| Memory recall (hybrid embedding + lexical, recency-decayed) | `services/memory.py`, `services/repo_kb.py` | `memory_recall` |
| Approved plan (interactive mode) | `services/plan_gate.py` | `plan_pending` / `plan_approved` / `plan_rejected` |

A resumed session gets only the delta (failure hint, notes) —
`runner/prompt.build_prompt`. Re-sending the full context on every retry is
what used to blow vendor rate limits.

- **Targeted retries** — `services/error_classifier.classify()` maps failures
  to an `ErrorClass(key, hint, severity)`; `hard` or a repeated class
  escalates. Event `error_classified`.
- **429 handling** — `runner/agent_call.py` retries the same call with
  `services/retry.retry_async` backoff; event `rate_limit_backoff` (carries
  `vendor`).
- **Budget circuit breaker** — `tasks.max_cost_usd` / `max_wall_seconds`
  (NULL = no limit); `services/budget.check_budget` → `budget_warning` at 80%,
  `budget_exceeded` → terminal `blocked`. Cost is not enforced in `max`
  billing mode (reports `cost_usd=0`); wall time counts only agent + verifier
  time.
- **Interactive mode / plan gate** — read-only planning call produces
  `task_runs.plan_json`; the dashboard approves/rejects via
  `POST /api/tasks/[id]/plan` (shared transition in `lib/task-steering.ts` —
  `decidePlan`; do not reimplement). The approved `files_to_touch` becomes an
  exhaustive allow-list enforced again by preflight.
- **PR preflight** — `services/pr_preflight.run_preflight` (deterministic, no
  LLM): author check (hard), plan allow-list (recoverable → retry with a
  scope-creep hint), secret scan via the shared `services/secret_rules.py`
  (hard), file size > 1 MB (hard). `PREFLIGHT_IGNORE_PATTERNS` skips globs.
  Events `pr_preflight_pass` / `pr_preflight_fail`.
- **Side-effect gate** (opt-in `side_effect_gate` setting) — an agent about to
  do something external POSTs `/internal/tasks/{key}/gate`; blocking gates open
  a `decision_points` row and suspend the task as `blocked` until resolved
  (`/api/decisions`, Telegram). `services/side_effect_gate.py`,
  `services/gate_classifier.py`.
- **Vendor failover** — `tasks.backup_vendor` / `backup_model`: after the
  primary exhausts retries, `phases._switch_to_backup` runs a second attempt
  loop on the backup (fresh session). Events `vendor_failover` /
  `backup_model_switch`.
- **Context compaction** — `services/compaction.compact_task` summarises recent
  runs with the system LLM into `tasks.compacted_context`, which then replaces
  the evidence blocks. Manual: `POST /api/tasks/[id]/compact`.
- **Continue** — `POST /internal/tasks/{key}/continue` marks in-flight runs
  failed (keeping `session_id`), drops the repo lock, sets `is_continuation`,
  resets to `pending`.
- **Task notes** — operator notes injected exactly once
  (`services/task_notes.py`, `/api/tasks/[id]/notes`).
- **Patch export** — `services/patch_export.py`: `git format-patch` per task
  into the patches dir; file names validated by strict regexes before any
  path is built. Worker `GET /internal/tasks/{key}/patches[/{file}]`, web
  `/api/tasks/[id]/patches[/[file]]`, UI `components/PatchesPanel.tsx`.
- **Outcome prediction** — `services/outcome.py` (repo baseline, or
  similar-task when embeddings are available); `GET /api/tasks/[id]/prediction`
  → `components/PredictionCard.tsx`.

- **Prompt hygiene** — `services/injection_scan.py` ↔ `lib/injection-scan.ts`
  (keep in sync; shared fixture `apps/worker/tests/fixtures/injection_cases.json`)
  strip invisible/bidi characters and flag override phrases at ingress
  (`POST /api/tasks`, webhooks, A2A, imports) → event `injection_sanitised`;
  never refuses. `runner/prompt.build_prompt` fences description, acceptance
  and failure evidence in entity-escaped `<untrusted_task>` /
  `<untrusted_evidence>` tags.
- **Action on completion** — `tasks.post_completion_action` (004) =
  `{kind: task|skill, task_id?|skill_name?, trigger}`;
  `services/post_completion.after_completion` runs after `finalise` and after
  `skill_runner` (outside the repo lock): `task` resets the target to pending,
  `skill` creates a skill task; both enqueue via `queue_bridge`. Never self,
  chain depth ≤ 3 via `triggered_by_task_id`, `cancelled` never fires. Event
  `post_completion_action`. Web validator `lib/post-completion-action.ts`.

All events flow through `task_events` → NOTIFY → WebSocket with no extra
plumbing; add new event types the same way (`runner/store.emit_event`).

## Agent backends and billing

`services/agent_backends.py`: an `AgentBackend` ABC (argv, env, output
parsing, 429 detection) with `register_backend()` and a typed `ModelInfo`
registry (`VENDOR_MODELS`) served at `GET /internal/agent-registry`. The web
app renders every picker from it (`lib/use-agent-registry.ts`,
`lib/agent-vendors.ts`) — the Python registry is the authority.

| vendor | class | binary | key env |
|---|---|---|---|
| `anthropic` | `ClaudeBackend` | `claude` | `ANTHROPIC_API_KEY` |
| `google` | `AntigravityBackend` | `agy` | `GEMINI_API_KEY` |
| `openai` | `OpenAIBackend` | `codex` | `OPENAI_API_KEY` |
| `glm` | `GLMBackend` (subclass of Claude) | `claude` → Zhipu endpoint | `GLM_API_KEY` |

Floating aliases (`claude-opus-latest`, `gpt-latest`, …) are resolved by
version at spawn time and when the System LLM setting is read
(`services/model_aliases.py`); see `docs/design-decisions.md`.

**Billing modes** (`tasks.claude_mode`, name kept for compat), applied by
`AgentBackend.build_env`: `api` passes the vendor key through; `max` strips it
so the CLI uses the host's OAuth/subscription login.

**System LLM** — `settings.system_llm_vendor` / `system_llm_model` /
`system_llm_mode`, read via `services/system_llm.py`; used by Fill Task,
DevPlan (`POST /internal/generate-plan`, optional `OBSIDIAN_FOLDER` export),
goal decompose/rollup, compaction and Ask Agent. `services/llm_client.py`
speaks the four vendor APIs; `max` mode runs through the vendor CLI instead.
GLM always uses HTTP.

## Feature map

### Inter-task messaging
`task_messages` table + NOTIFY. `services/task_messaging.py` (`send_message`,
`read_inbox`, `list_sessions`, `thread`); worker `routes/messaging.py`
(`/internal/tasks/{key}/messages/{send,inbox,thread}`, `/internal/sessions/list`);
web `/api/tasks/[id]/messages[/as-operator]`. Agents get
`DEVSERVER_WORKER_URL` + `DEVSERVER_TASK_KEY` in their env. Destination
`operator` fans out to Telegram (`/inbox`, `/reply`). Bodies are secret-screened
(`message_redacted`).

### Webhook triggers
`webhook_triggers` rows with a random token → public `POST /api/webhooks/{token}`.
Entirely in the web tier: `apps/web/src/lib/webhooks/` (HMAC verification
against `x-devserver-signature` / `x-hub-signature-256` / `x-gitea-signature`
with constant-time compare, `{{path}}` template rendering, task insert,
`enqueueTask`). Admin CRUD `/api/webhook-triggers[/[id]]` (never returns the
secret), UI `/webhooks`. Tested via an injected fake store (`npm test`).

### Memory and knowledge base
- `services/embeddings.py` — fastembed, `EMBEDDING_MODEL` (default
  `BAAI/bge-base-en-v1.5`, **768-dim — must match the vector columns**).
  Backfill: `apps/worker/scripts/reembed_memory.py`.
- `services/memory.py` — `agent_memory` storage/recall: hybrid recall,
  decay + archive (`memory_archive` scheduler job), decisions, iterative
  multi-hop recall (opt-in).
- `services/repo_kb.py` — `RepoMemory(session, repo_id)`, the per-repo seam:
  recall, transcript drawers, wake digest (`repo_memory` table).
- `services/memory_facts.py` — temporal facts (`memory_facts`, supersession).
- `services/corpus.py` — semantic search over repo files (`corpus_chunks`,
  content-addressed by `file_sha`, exact cosine scan by design — no ivfflat).
- Worker routes: `routes/memory_kb.py` (agent memory lanes, repo-map
  drill-down, `/internal/repos/{id}/corpus/{search,stats,ingest}`).

### Notifications (`services/notify/`)
`NotifyBackend` ABC (`base.py`; implement `send_text`, optionally rich
per-event methods). `notify = Dispatcher()` in `__init__.py` fans out in
parallel; a failing backend never blocks siblings. Backends:
`telegram_backend.py` (rich formatting in `services/telegram_rich.py`),
`discord_backend.py` (`DISCORD_WEBHOOK_URL`), `a2a_backend.py` (per-task push
configs; inert until a peer registers one). New channel = subclass + add to the
dispatcher + document env vars in `config/.env.example` and `config.py`.

### Telegram commands (`services/telegram_polling.py`)
`/status`, `/approve`, `/reject`, `/cancel`, `/retry`, `/reply TASK-KEY msg`,
`/inbox [N]`, `/pause`, `/resume`, `/mode autonomous|interactive`,
`/budget TASK-KEY`, `/help`. `getUpdates` is exclusive per bot token: use a bot
per instance (commands addressed as `/cmd@name`, matched against the bot
username or `settings.instance_label`) or set `TELEGRAM_POLLING_ENABLED=false`
on all but one instance; 409s back off via `_CONFLICT_BACKOFF`.
`INSTANCE_NAME` prefixes every message.

### A2A gateway (`003_a2a.sql`)
DevServer as an A2A 1.0 peer, **in the Next.js tier only** (the worker is
unauthenticated). `/.well-known/agent-card.json` (rewrite in `next.config.ts`
→ `/api/a2a/card`; `middleware.ts` skips `/.well-known/`), JSON-RPC at
`POST /api/a2a` (streaming methods return SSE from the same route), admin CRUD
`/api/a2a-peers`, UI `/a2a` (`components/A2APeersView.tsx`).
`apps/web/src/lib/a2a/`: `protocol.ts` (wire format is lowercase kebab —
`"input-required"`, not proto enum names), `auth.ts` (SHA-256 token lookup,
atomic `consumeTaskQuota`), `mapping.ts` (state mapping; `test`/`done` →
`completed`, `blocked` or an undecided plan → `input-required` via
`isAwaitingPlanDecision`), `push.ts` (**SSRF guard: resolves and checks every
address**), `redact.ts` (TS port of `services/secret_rules.py` — **keep the two
in sync**), `stream.ts`, `card.ts`, `steer.ts`. Peer defaults are the safest
combination (no repos, research-only, plan approval required, `patch` flow).

### Ask Agent panel
Floating chat drawer on every page, backed by the System LLM. Path: browser
`POST /api/assistant/chat` → `lib/assistant-proxy.ts` (forwards
`request.signal`, fresh headers, errors as in-band events on a 200) → worker
`POST /internal/assistant/chat/stream`. Event vocabulary defined once in
`services/llm_stream.py`. Knowledge is the curated `docs/assistant/` set
selected by `services/assistant_kb.py` — not RAG over README/CLAUDE.md. Pages
register on-screen entities with `useAssistantPageContext(...)`. Sessions are
in-memory on the worker; the transcript is kept browser-side
(`lib/ask-agent-history.ts`, masked, never the session id) and resent as
`history` when a conversation is reopened; the system-LLM read falls back to `.env` when
Postgres is down.

Two engines:
- **chat** (default) — plain streaming chat, no tools, no side effects.
- **agent** (opt-in `assistant_agent_mode` setting; Anthropic/GLM only) —
  `services/assistant_agent.py` runs the vendor CLI with read-only tools
  (Read/Glob/Grep) plus the DevServer MCP server (`services/assistant_mcp.py`
  writes its config; no token in it). Tool previews and text deltas are
  redacted via `services/secret_rules.py`. Anything state-changing is only
  *proposed* (`propose_action` → `services/assistant_actions.py`): create /
  run / cancel a task, or an allow-listed script (restart, migrate, dbrestart,
  backup). Nothing runs until the operator approves through
  `POST /api/assistant/actions/[id]/decide` (explicit body required). Script
  actions are refused unless `INTERNAL_API_TOKEN` is set (`X-Internal-Token`);
  an interrupted action is marked failed, never left `approved`.

### Session observatory (`/sessions`)
Read-only view of every Claude Code and Codex session on the worker host.
`services/claude_sessions.py` (index = `~/.claude/projects/**/*.jsonl`;
byte-offset tailing; `enrich_sessions` attributes sessions to tasks/repos;
`delete_session` refuses live sessions and containment-checks every path
against the resolved `CLAUDE_HOME`), `services/codex_sessions.py`
(`$CODEX_HOME/sessions`, list + tail only). Worker `routes/sessions_night.py`;
web `/api/claude-sessions/**`, `/api/codex-sessions/**`,
`components/SessionsView.tsx`. Text is capped then redacted via
`services/secret_rules.py`. In Docker, bind-mount `~/.claude` / `~/.codex`
read-only and set `CLAUDE_HOME` / `CODEX_HOME`.

### Dashboard shell (Quick-Wins Wave 1)
- **Request plumbing** — components call `lib/api-client.ts` (`api.get/post/…`,
  typed `ApiError`) and `lib/use-async.ts` (`useAsyncAction`, `useResource`:
  stale responses dropped, poll paused while hidden, reload on connectivity
  restored). Route handlers use `lib/route-utils.ts` (`parseRouteId` +
  `invalidId()` → 400, `jsonError`, `proxyWorker` → worker detail forwarded,
  unreachable → 502).
- **Command palette** — ⌘/Ctrl+K (`components/CommandPalette.tsx`, ranking in
  `lib/command-palette.ts`, shortcut table `lib/shortcuts.ts`, nav list
  `lib/nav-items.ts`); ⌘/Ctrl+J toggles Ask Agent; `?` lists shortcuts.
- **Needs You pill** — `GET /api/needs-you` (blocked + undecided plans);
  `lib/needs-you.ts` is one shared poll per tab.
- **Connectivity banner** — `lib/connectivity.ts` state machine,
  `GET /api/health/connectivity`, `components/ConnectivityBanner.tsx`;
  `NotificationProvider` suppresses toasts the banner explains.
- **Update notice** — worker `services/release_check.py`
  (`GET /internal/update/status`, GitHub releases, 6 h cache, failure →
  unknown), web `/api/version/update[/dismiss]`, `components/UpdateNotice.tsx`.
  Settings `update_notify_enabled` (default true), `update_dismissed_build`.
- **Tasks search / linkify / EmptyState** — `lib/task-search.ts` (≤ 8 terms,
  all must match), `lib/linkify.ts` (http(s) only), `components/EmptyState.tsx`.
- **Analytics** — tokens/s per vendor and tokens per task from `cost_update`
  event payloads (`lib/analytics-metrics.ts`), `AnalyticsStatisticsModal.tsx`.
- **Ask Agent history** — `lib/ask-agent-history.ts` (20 conversations in
  localStorage, secrets masked, no session id) and `lib/ask-agent-composer.ts`.
- **Cockpit tokens** — `src/app/cockpit.css`, imported after `globals.css`.
- **Feature catalogue** — `docs/feature-catalogue.md`; tests carry
  `// Feature: F-…` / `# Feature: F-…`; `node scripts/feature-coverage.mjs`
  (CI, non-blocking).

### Night cycle
`services/night_cycle.py`, worker `/internal/night-cycle/{start,stop,status}`,
web `/api/night-cycle`, `components/NightCyclePanel.tsx`.

### MCP server (`apps/mcp-memory/`)
`devserver_memory_mcp/server.py`, stdio by default, for an interactive CLI.
Task control: `task_options`, `task_create`, `task_run`, `task_list`,
`task_status`, `task_output`, `task_cancel` — thin `httpx` proxies to the
worker's `/internal/agent-registry` and `/internal/tasks/*` (`routes/tasks.py`,
`routes/task_actions.py`). `task_create` defaults the repo from the console's
git toplevel. Needs `DEVSERVER_WORKER_URL`, and the web tier running to
actually enqueue. Memory tools expose the repo knowledge base (`wake_digest`,
`memory_recall`, `memory_facts`, `memory_decisions`, `memory_transcripts`,
`repo_map`, `code_search`, `doc_search`, `corpus_*`, `memory_remember`,
`record_fact`, `record_decision`); `propose_action` backs the Ask Agent engine.

Remote mode (`remote.py`): `DEVSERVER_MCP_TRANSPORT=http`
(`DEVSERVER_MCP_HOST` / `DEVSERVER_MCP_PORT`, default `127.0.0.1:8900`)
serves Streamable HTTP; every request needs an A2A peer bearer token,
introspected via `DEVSERVER_WEB_URL`, and is scoped to the peer's repos.
`task_run` and `propose_action` are refused remotely. It speaks **plain
HTTP** — put TLS in front of it whenever the host is not localhost. Expose
this server, never the worker.

### Also
Task templates (`/templates`), Ideas / goal trees (`/ideas`,
`/internal/goals/{id}/{expand,rollup}`), scheduled jobs (`/jobs`,
`/api/schedules`), Confluence import (`services/import_sources/`,
`/api/import/*`), dashboard analytics (`/api/analytics`,
`components/DashboardCharts.tsx`), Setup wizard + Settings env editor
(`routes/env_config.py`, `DEVSERVER_ENV_FILE`), backup/restore scripts
(`docs/backup-restore.md`).

## Deferred

Intentionally not built: parallel sub-agents per task, learned rules from
review reactions, a sandboxed container per task (worktree + repo lock
isolation covers the current threat model), a typed code graph (the repo map
covers most of the value).
