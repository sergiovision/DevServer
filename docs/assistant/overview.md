# What DevServer is

DevServer is an autonomous development pipeline. You describe a job; DevServer
hands it to a coding-agent CLI (Claude Code, Codex, Antigravity or GLM) running
in an isolated git worktree, verifies the result, and opens a pull request.

## The pipeline

1. **You create a task** — a title, a description, and which repo it applies to.
2. **It is queued.** DevServer uses a PostgreSQL-backed job queue, so nothing is
   lost across restarts. One task per repo runs at a time (a per-repo lock).
3. **The agent runs** in a private git worktree, so it can never disturb your
   working copy. It gets a repo map, prior context, and your acceptance criteria.
4. **The verifier runs** the repo's build / test / lint commands. If they fail,
   the agent is given the classified error and retries — targeted, not a blind
   re-run.
5. **A pull request is opened** (or a patch file produced, depending on the git
   flow you chose).

Statuses move `pending → queued → running → verifying`, then finish as
`test` (succeeded, awaiting your review), `failed`, `blocked` (needs a human)
or `cancelled`.

## The screens

| Screen | What it is for |
|---|---|
| **Dashboard** (`/`) | What is running and queued right now, plus cost/duration analytics |
| **Tasks** (`/tasks`) | The full backlog, filterable; grid or pipeline-board view |
| **Task detail** (`/tasks/{id}`) | Live event timeline, runs, logs, agent settings, patches |
| **Templates** (`/templates`) | Saved task presets for repetitive work |
| **Ideas** (`/ideas`) | A hierarchical brainstorm tree; nodes convert into tasks |
| **Jobs** (`/jobs`) | Cron schedules that re-run tasks |
| **Repos** (`/repos`) | Repositories DevServer can work in |
| **Settings** (`/settings`) | Queue controls, the system LLM, and the `.env` editor |
| **Logs** (`/logs`) | Live tail of the worker and web process output |

## The two processes

DevServer runs as two services plus a database:

- **web** — the Next.js dashboard (default port 3200). It also owns the queue:
  it is the only thing that writes jobs onto the queue.
- **worker** — the Python FastAPI service (default port 8000) that actually runs
  agents, verifies, and talks to Gitea/GitHub.
- **PostgreSQL** — state, events, and the memory/vector store (pgvector).

Both processes must be running for a task to start. Live updates reach your
browser over a WebSocket, so the dashboard reflects events as they happen
without a refresh.
