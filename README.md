<div align="center">

# DevServer

### Give it a task. Get back a pull request.

**The open-source MVP of an autonomous coding pipeline. It runs Claude Code, Codex, Gemini (Antigravity) or GLM on your own machine, checks their work with your build, tests and linter, and hands you a pull request. Clone it. Change it. Make it yours.**

[![CI](https://github.com/sergiovision/DevServer/actions/workflows/ci.yml/badge.svg)](https://github.com/sergiovision/DevServer/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.14](https://img.shields.io/badge/Python-3.14-3776AB?logo=python&logoColor=white)](https://python.org/)
[![Next.js 15](https://img.shields.io/badge/Next.js-15-black?logo=next.js&logoColor=white)](https://nextjs.org/)
[![PostgreSQL 17](https://img.shields.io/badge/PostgreSQL-17-4169E1?logo=postgresql&logoColor=white)](https://postgresql.org/)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker&logoColor=white)](docker/)

**DevServer grew up. Its name is now [DevAgentForce →](https://capitaltools.app/devagentforce)**

<img src="assets/viewtask.png" alt="DevServer task detail view — live event log, run history, and the agent settings sidebar" width="90%" />

[The idea](#the-idea) · [Make it yours](#start-here-make-it-yours) · [DevAgentForce](#then-it-grew-up) · [Engineering](#engineering-highlights) · [Features](#features) · [Architecture](#architecture) · [Quick Start](#quick-start) · [Docs](#documentation) · [Roadmap](#roadmap)

</div>

---

## The idea

The best coding agents in the world live in a terminal. They are astonishing. And they have one problem.

They need you. One task at a time, one window at a time, while you sit there and watch.

So I asked a simple question. What if you could just hand over the work?

Give an agent a task. Give it its own copy of the code. Run your build, your tests and your linter. If the work passes, open a pull request. If it fails, tell the agent exactly why, and let it try again.

That's the whole idea. Task in. Pull request out.

<table>
<tr>
<td width="50%"><img src="assets/logo/BeforeDevServer.jpg" alt="Three unharnessed horses standing idle in a field, branded with the Gemini, Claude and ChatGPT logos" /><br/><sub><b>Before.</b> All that horsepower, standing in a field.</sub></td>
<td width="50%"><img src="assets/logo/WithDevServer.jpg" alt="A black horse rearing, fitted with a DevServer saddle and branded with the three AI provider logos" /><br/><sub><b>After.</b> The same horsepower, with a harness.</sub></td>
</tr>
</table>

## Start here. Make it yours.

DevServer is a minimum viable product, on purpose. Minimum doesn't mean unfinished. It means everything that wasn't essential was left out, and everything that's left works: the queue, the isolated worktree, the agent, the verifier, the pull request.

Every team builds software differently. Your checks aren't my checks. Your rules aren't my rules. So a pipeline you can't change is a pipeline you'll eventually stop using.

That's why DevServer is MIT licensed and written to be read. It's the foundation for your own autonomous coding pipeline. Fork it, then change the parts that make your team yours.

| You want to | Start here |
|---|---|
| Add an agent vendor | `AgentBackend` in [`agent_backends.py`](apps/worker/src/services/agent_backends.py) |
| Decide what "done" means | [`verifier.py`](apps/worker/src/services/verifier.py) |
| Decide what must never ship | [`pr_preflight.py`](apps/worker/src/services/pr_preflight.py) |
| Shape what the agent knows before it starts | [`runner/evidence.py`](apps/worker/src/services/runner/evidence.py), [`runner/prompt.py`](apps/worker/src/services/runner/prompt.py) |
| Start work from your own tools | Webhook triggers, cron, or the [MCP server](apps/mcp-memory/README.md) |

### What's already in the box

Most autonomous coding agents ship as a closed SaaS, a VS Code extension, or a CLI glued to GitHub. DevServer is the opposite: a **self-hosted harness** that runs the coding-agent CLIs you already use as supervised workers, with a queue, a verifier, guardrails, an audit trail and a dashboard around them.

- **Multi-vendor agent backends.** Claude (Anthropic), Gemini via Google's Antigravity CLI, Codex (OpenAI) and GLM (Zhipu), behind one `AgentBackend` interface. Switch per task; fail over to a backup vendor or model automatically when retries run out.
- **Pay by API key or by subscription.** Per-task billing mode: `api` (metered key) or `max` (Claude Max, ChatGPT Plus, Google AI Pro/Ultra through the CLI's own login).
- **Evidence before action.** A repo map, a 0–100 reality score and hybrid memory recall go into the prompt before the agent writes a line; an opt-in abstain gate blocks tasks the code base cannot support.
- **Error-class-aware retries.** Failures are classified by 20+ rules and the next attempt gets a targeted hint. The same error twice escalates instead of burning retries.
- **Guardrails without extra LLM calls.** Budget circuit breaker, deterministic PR preflight (secrets, forbidden and oversize files, author), side-effect approval gate.
- **Human in the loop where it matters.** Plan approval for interactive tasks, operator notes delivered to a running agent, real cancellation that kills the agent process.
- **Full live observability.** Every step is a typed event: PostgreSQL `NOTIFY` → WebSocket → dashboard, plus opt-in OpenTelemetry export.

All of the above are real, tested code paths. The sections below and the [`docs/`](docs/) folder show how they are built.

## Then it grew up.

DevServer proved the idea. Then the real work began.

The worker was rewritten in Go. The setup shrank to one command. Secrets went into a sealed vault. Every agent on the machine came into one cockpit, where you can steer it with a sentence. Every repository got a memory. And it got a new name.

**[DevAgentForce](https://capitaltools.app/devagentforce)** is the product DevServer grew into. Claude, Gemini and Codex are the engines. DevAgentForce is the harness. It runs on your hardware, and your code goes straight from you to your AI vendor. No one in the middle.

| | DevServer | DevAgentForce |
|---|---|---|
| **What it is** | The open-source MVP | The finished product |
| **Made for** | Building your own pipeline | Shipping with agents today |
| **Price** | Free, MIT licensed | Free edition. Pro is $15 a month |
| **Source** | Open. Read it, fork it | Proprietary |
| **Install** | Clone, then set up Node, Python and PostgreSQL | One command, or a native desktop app. The database comes with it |
| **Worker** | Python | Go |
| **Agents** | Claude, Codex, Gemini, GLM | Claude, Codex, Gemini |
| **Pay with your AI subscription** | Yes | Yes, with a paid licence |
| **Git** | GitHub, Gitea, any local folder | Those, plus Azure DevOps |
| **Reality gate, plan approval, budget breaker, PR preflight** | Yes | Yes, in Pro |
| **Code review** | Yours | A reviewer agent inspects every change, round after round (Pro) |
| **Secrets** | `.env` files | Encrypted vault and redaction. Per-task scoping in Pro |
| **Steering a running agent** | Notes, delivered to the agent | A live Sessions cockpit across every agent on the machine (Pro) |
| **Memory** | Recall of past tasks | Facts, decisions and every past attempt, per repo (Pro) |
| **MCP** | Create and run tasks from your CLI | Memory, code search and the task queue from your CLI (Pro). Attach external MCP servers, every call on the record |
| **Interface** | English | 9 languages |

**[Compare the editions](https://capitaltools.app/devagentforce#editions) · [Install it free](https://capitaltools.app/devagentforce#install) · [Buy Pro](https://capitaltools.app/devagentforce#pricing)**

### Looking at Cursor, Devin or JetBrains Air?

They're good tools. Look at this one too.

Cursor gives you an editor. Devin gives you an engineer in someone else's cloud. JetBrains Air gives you a desktop app to delegate from.

DevAgentForce gives you a factory. It's yours, and it runs on your machine. Keep the editor you love, and the Claude Code, Codex or Antigravity CLI you already pay for. Hand it a backlog. Every task gets its own worktree, your own build and tests, and a pull request at the end. Hit a rate limit, and another vendor takes over mid-task. We never resell AI. You pay us for the harness, and your AI vendor for the horsepower.

**[See it working →](https://capitaltools.app/devagentforce#screenshots)**

### Hiring? Leading an engineering team?

This is what one engineer can ship in the AI age.

I'm Sergei Zhuravlev. I started DevServer in April 2026. Seven months later it's a commercial product: a licence server, a checkout, an installer for macOS, Linux and Windows, a native desktop app in Rust, an interface in nine languages. DevAgentForce is built with DevAgentForce.

You don't have to take my word for it. The code is right here. Start with [Engineering highlights](#engineering-highlights), then read the [design decisions](docs/design-decisions.md). Then let's talk: [LinkedIn](https://www.linkedin.com/in/sergeizhuravlev/) · [hi@sergego.com](mailto:hi@sergego.com).

## Engineering highlights

DevServer is also a showcase of how to build an **agent harness** that stays readable as it grows. The priorities, in order: **readability → reviewability → correctness → features**. Performance is deliberately not a goal.

| Concern | How it is solved | Where |
|---|---|---|
| A 1,600-line `run_task` | A ~30-line `run_task` orchestrating small phase functions sharing one `TaskContext`; primary and backup models go through the *same* `run_attempts()` | [`agent_runner.py`](apps/worker/src/services/agent_runner.py), [`services/runner/`](apps/worker/src/services/runner/) |
| "Did the refactor change behaviour?" | Characterisation tests drive the real runner against real PostgreSQL, a temp git repo and a scripted fake agent subprocess | [`tests/runner/`](apps/worker/tests/runner/) |
| Cancel only relabelled the task | A `RunningTasks` registry cancels the `asyncio.Task`; `proc.run` kills the agent's whole process group; the runner always writes a terminal status | [`running_tasks.py`](apps/worker/src/services/running_tasks.py), [`proc.py`](apps/worker/src/services/proc.py), [`task_control.py`](apps/worker/src/services/task_control.py) |
| Locks that could be stolen or leak | Repo locks are **leases**: an owner, a short TTL and a heartbeat; release deletes only the caller's row; a busy repo re-queues the job instead of failing it | [`repo_lock.py`](apps/worker/src/services/repo_lock.py), [`queue_consumer.py`](apps/worker/src/services/queue_consumer.py) |
| Model IDs copied across Python and TypeScript | One typed registry (`ModelInfo`) served at `/internal/agent-registry`; the web app renders every picker from it | [`agent_backends.py`](apps/worker/src/services/agent_backends.py), [`agent-vendors.ts`](apps/web/src/lib/agent-vendors.ts) |
| Four hand-rolled retry loops | One `retry_async(fn, attempts=, backoff=Backoff(...), retry_on=)` | [`retry.py`](apps/worker/src/services/retry.py) |
| DB sessions pinned for an hour per task | Repository-style functions, one short session per unit of work; nothing holds a session across a subprocess | [`runner/store.py`](apps/worker/src/services/runner/store.py) |
| Hung git remotes | Every git call takes a required, per-operation timeout | [`git_ops.py`](apps/worker/src/services/git_ops.py) |

Tooling: `ruff` (lint + format, blocking), `pyright` basic (reported), `pytest` with real-database integration tests, Node's test runner for the web's pure modules, all run by [GitHub Actions](.github/workflows/ci.yml) on every push. The design walkthrough is in [`docs/architecture.md`](docs/architecture.md) and the reasoning behind it in [`docs/design-decisions.md`](docs/design-decisions.md).

## Features

<table>
<tr>
<td width="50%"><a href="assets/dashboard.png"><img src="assets/dashboard.png" alt="Dashboard — running agents, queue controls, cost and success-rate analytics" /></a><br/><sub><b>Dashboard</b>: live queue, cost per PR, success rate</sub></td>
<td width="50%"><a href="assets/tasks.png"><img src="assets/tasks.png" alt="Tasks list — priority, status, repo, turns" /></a><br/><sub><b>Tasks</b>: backlog grid or a kanban pipeline board</sub></td>
</tr>
</table>

| Area | What you get |
|---|---|
| **Task pipeline** | Queue → isolated worktree → agent CLI → verify (build/test/lint) → PR preflight → PR, direct commit, `.patch` files or untracked changes |
| **Repositories** | GitHub (incl. Enterprise), Gitea/Forgejo, or **Local Git**: any local clone, no tokens, never pushes |
| **Agents** | Claude, Codex, Gemini via Antigravity, GLM; per-task vendor/model, backup model for auto-failover, API-key or subscription billing |
| **Guardrails** | Reality gate (0–100 evidence score), budget circuit breaker, secret/forbidden-file preflight, Plan Gate approval |
| **Human in the loop** | Plan approval, Task Notes delivered to a running agent, real cancellation, Telegram/Discord notifications |
| **Memory** | Hybrid pgvector + full-text recall of past tasks, fused with Reciprocal Rank Fusion, local embeddings |
| **Automation** | Cron jobs, HMAC-signed webhook triggers, Confluence import, MCP server for Claude Code / Codex / Gemini |
| **Observability** | Typed events over `NOTIFY` → WebSocket, live logs, analytics, opt-in OpenTelemetry export |
| **Product extras** | Task templates, an ideas tree, a Mermaid module diagram per repo, an *Ask Agent* help assistant |

Every screen and subsystem, with screenshots and source links: **[docs/features.md](docs/features.md)**.

## Architecture

```mermaid
flowchart TB
  subgraph browser["Browser"]
    Dash["Dashboard · CoreUI"]
  end

  subgraph web["Next.js · apps/web/"]
    API["API routes"]
    WS["WebSocket server"]
    Prod["PgQueuer producer"]
    Hook["Webhook receiver<br/>(HMAC-verified)"]
  end

  subgraph worker["FastAPI worker · apps/worker/"]
    Cons["Queue consumer<br/>+ RunningTasks registry"]
    Orch["agent_runner.run_task<br/>(orchestrator)"]
    subgraph runner["services/runner/"]
      direction LR
      Prep["prepare_workspace"] --> Ev["gather_evidence<br/>repo map · reality · memory<br/>(plan gate)"] --> Att["run_attempts<br/>primary → backup"] --> Out["outcome"]
    end
    Lock["repo_lock<br/>(lease + heartbeat)"]
    Orch --> Lock
    Orch --> runner
  end

  subgraph ext["External"]
    direction TB
    Git[("Gitea / GitHub /<br/>local folder")]
    PG[("PostgreSQL 17<br/>+ pgvector")]
    Agents["Claude · Codex · agy · GLM CLIs"]
    TG["Telegram / Discord"]
  end

  Dash <--> API
  Dash <--> WS
  Hook --> Prod
  API --> Prod --> PG
  PG --> Cons --> Orch
  PG -- NOTIFY --> WS
  Att --> Agents
  Att --> Git
  Out --> TG
```

Three small services, one PostgreSQL. No Redis, no RabbitMQ, no Celery — **PgQueuer** uses the same database everything else lives in.

### One attempt

```mermaid
flowchart LR
  B{"budget<br/>check"} -->|ok| N["take operator notes"] --> P["build prompt<br/>(full, or delta on resume)"] --> A["agent CLI"]
  A -->|exit 0| V["verify<br/>pre · build · test · lint"] -->|pass| F["PR preflight"] -->|pass| H["hand over<br/>PR · commit · patch · untracked"]
  A -->|failed| C["classify error"] --> R{"retry or<br/>escalate?"}
  V -->|fail| C
  F -->|scope creep| R
  F -->|hard violation| X["blocked"]
  B -->|exceeded| X
```

Every exit of an attempt is a `Verdict` (`SUCCEEDED`, `RETRY`, `ESCALATE`, `BLOCKED`, `BUDGET_EXCEEDED`, `SUSPENDED`); the attempt loop and [`outcome.record`](apps/worker/src/services/runner/outcome.py) are a `match` over that enum. A walkthrough of every module is in [`docs/architecture.md`](docs/architecture.md).

## Tech Stack

| Layer | Choice | Why |
|---|---|---|
| **Frontend** | Next.js 15 App Router · React 19 · CoreUI Pro | Server components for task pages, client components for real-time panels. |
| **Backend worker** | Python 3.14 · FastAPI · SQLAlchemy 2.0 async · asyncpg | Async from top to bottom — every subprocess, DB call, and agent invocation is non-blocking. |
| **Job queue** | [PgQueuer](https://github.com/janbjorge/pgqueuer) | PostgreSQL-native queue. No Redis, no RabbitMQ — one fewer service to monitor. |
| **Database** | PostgreSQL 17 | Relational truth + queue + real-time notifications in one store. |
| **Real-time** | `LISTEN/NOTIFY` → WebSocket | Zero-dependency pub/sub. Dashboard updates arrive within ~100 ms. |
| **AI engines** | Claude, Gemini, Codex, GLM CLIs | DevServer *orchestrates* existing CLIs instead of reimplementing agent logic. |
| **Embeddings** | `fastembed` (local, ONNX/CPU) | Local semantic embeddings for memory recall — no cloud API, no key. Default `BAAI/bge-base-en-v1.5` (768-dim), override via `EMBEDDING_MODEL`. |
| **Git platform** | Gitea / Forgejo / GitHub / Local Git | Gitea and GitHub get PRs via their REST APIs; Local Git repos work on a plain local clone — any provider, no API, no tokens. |
| **Notifications** | Telegram Bot API | Basic task lifecycle alerts. |
| **Charts** | Chart.js + react-chartjs-2 | Lightweight, no-frills analytics visualizations. |
| **Package mgmt** | `uv` (Python) · `npm` (Node) | Fast, cacheable, boring. |

## Quick Start

Prerequisites: Node.js 22+, Python 3.14+, PostgreSQL 16+ with `pgvector`, [`uv`](https://docs.astral.sh/uv/), and at least one agent CLI signed in (e.g. `claude login`).

```bash
git clone https://github.com/sergiovision/DevServer.git
cd DevServer
cp config/.env.example .env   # minimum: PGPASSWORD and one vendor key (or a CLI subscription login)

./scripts/migrate.sh          # idempotent schema migration
./scripts/start.sh --dev      # worker + web in dev mode
```

Open **http://localhost:3200**. To run with Docker instead, use `./scripts/start.sh --docker`.

The full guide covers both Docker topologies, host PostgreSQL, subscription logins inside containers, Windows (native PowerShell or Docker Desktop) and upgrades: **[docs/quick-start.md](docs/quick-start.md)**.

## Development

```bash
cd apps/worker
uv sync
uv run ruff check . && uv run ruff format --check .   # blocking
uv run pyright                                         # basic mode, reported
uv run pytest                                          # needs a local PostgreSQL for tests/runner
```

The integration tests create and migrate `devserver_test` on first use (override with `DEVSERVER_TEST_DATABASE_URL`). Without a reachable server they are skipped and the unit tests still run.

```bash
cd apps/web
npm test              # webhook template / signature / handler tests
npx tsc --noEmit
npm run build
```

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs the same commands against a `pgvector/pgvector:pg17` service. See [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request.

## Project Layout

```
apps/
  web/          → Next.js dashboard, API routes, PgQueuer producer, WebSocket relay, webhook receiver
  worker/       → FastAPI worker: queue consumer, task runner (src/services/runner/), agent backends, guardrails
  mcp-memory/   → stdio MCP server that lets coding CLIs create and run DevServer tasks
database/       → idempotent SQL migrations
docker/         → one Dockerfile (--target web|worker|pydeps) + compose files
scripts/        → build / start / stop / migrate / backup / restore (bash and PowerShell)
docs/           → architecture, design decisions, features, quick start, backup
```

A file-by-file map of the runner is in [`docs/architecture.md`](docs/architecture.md).

## Documentation

| Document | Contents |
|---|---|
| [Quick Start](docs/quick-start.md) | Local, Docker and Windows setup; upgrading |
| [Features](docs/features.md) | Every screen and subsystem, with screenshots |
| [Architecture](docs/architecture.md) | How a task becomes a pull request, module by module |
| [Design Decisions](docs/design-decisions.md) | Multi-vendor backends, failover, error-class retries, rate limits, OpenTelemetry, patterns |
| [Backup & Restore](docs/backup-restore.md) | Full-system backup and restore scripts |
| [Releases](https://github.com/sergiovision/DevServer/releases) | Release notes per version |

## Roadmap

**Shipped.** Everything above is implemented and covered by tests.

**Next:**

- Make `pyright` blocking once the remaining basic-mode findings in older modules are fixed.
- GPT-6.1 Sol / Terra, GPT-6 Luna and GPT-6 Pro in the registry as soon as the Codex CLI catalogue lists them.

**Intentionally deferred:**

- **Parallel sub-agents per task** — git worktrees are already per-task; sub-worktrees add complexity with unclear value at this scale.
- **Sandboxed container per task** — overlaps with the existing worktree + repo-lock isolation.
- **Codebase-as-typed-graph** — the repo map captures most of the value at a small fraction of the effort.

Contributions and issues are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

## One more thing

Everything on this page is the beginning of the story. The rest of it takes one line:

```bash
curl -fsSL https://capitaltools.app/install.sh | bash
```

It downloads, verifies, starts and opens the dashboard. No account. No database to prepare. On Windows, or if you'd rather have a native app, [the installers are here](https://capitaltools.app/devagentforce#install). Want all of Pro first? [Generate a 14-day trial key](https://capitaltools.app/account/trial?product=devagentforce).

**All the horsepower of AI. Finally harnessed. [capitaltools.app/devagentforce →](https://capitaltools.app/devagentforce)**

## License

[MIT](LICENSE) — free for personal and commercial use. Attribution appreciated but not required.

---

<div align="center">

### Built by Sergei Zhuravlev
[DevAgentForce](https://capitaltools.app/devagentforce) · [LinkedIn](https://www.linkedin.com/in/sergeizhuravlev/) · [GitHub](https://github.com/sergiovision) · [hi@sergego.com](mailto:hi@sergego.com)
</div>
