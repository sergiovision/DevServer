# Troubleshooting

## The database is down

Symptoms: pages show a red database banner, the dashboard is empty, API calls
return 503 with a `db` diagnostic.

Fix: run `scripts/dbrestart.sh` on the worker machine. It handles all three
deployment shapes — the bundled `devserver-postgres` container, a host service
(launchd/systemd), or a raw data directory via `pg_ctl` — and it clears a stale
`postmaster.pid`, which is the usual reason Postgres refuses to start with
*"lock file postmaster.pid already exists"* and then crash-loops.

```bash
./scripts/dbrestart.sh
```

Check `PGHOST`/`PGPORT` in `.env` if it comes back up but DevServer still cannot
connect.

## A task is stuck in `queued`

- Is the queue paused? Check Settings, or `/status` in Telegram.
- Is another task holding that repo's lock? One task per repo runs at a time.
- Is the **web** process running? It is the only thing that writes to the queue,
  so a task created while it is down never gets dispatched.
- Is the **worker** running? It is the only thing that consumes the queue.

## A task is `blocked`

`blocked` is deliberate, not a crash. The task detail page states the reason:

- **PR preflight failed** — a secret was detected, a file exceeds 1 MB, or the
  HEAD commit is not authored by the configured git identity. Nothing was
  pushed.
- **Budget exceeded** — the task hit its cost or wall-clock ceiling.
- **Reality abstain** — the evidence score was below your threshold, so
  DevServer refused to spend an agent run.
- **Plan rejected or timed out** — an interactive task whose plan nobody
  approved within the window.

Fix the underlying cause, then **Continue** (keeps the session) or **Re-run**.

## Rate limits (429)

DevServer retries inside the same call with backoff and emits
`rate_limit_backoff` events. If it happens constantly: run fewer tasks
concurrently against one vendor, or set a **backup vendor** so tasks fail over
to a different provider automatically.

## An agent CLI is "not found"

The CLI for the task's vendor is not installed on the worker machine, or not on
its `PATH`. Install it, or switch the task to a vendor you do have. In
subscription (Max) billing you also need to have logged that CLI in
interactively on the host at least once.

## The agent ran out of turns

Raise **Max turns** on the task, or use **Continue**, which resumes the same
agent session rather than starting over. If it keeps happening, the task is
usually under-specified — tighten the description and acceptance criteria.

## A long task has gone in circles

Use **Compact**: it summarises the run history into a short brief (what was
tried, what is already dead, what to do next) and restarts the agent on a clean
conversation.

## Restarting DevServer

```bash
./scripts/restart.sh --dev     # or --prod, or --docker
./scripts/start.sh             # start only
./scripts/stop.sh              # stop and sweep orphans
./scripts/migrate.sh           # apply database migrations
```

Worker and web logs are tailed live at `/logs`, and written to `logs/worker.log`
and `logs/web.log`.
