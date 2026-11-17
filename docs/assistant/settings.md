# Settings and environment

**Settings** (`/settings`) holds three cards.

## General

- **Global mode** — `autonomous` (tasks run straight through) or `interactive`
  (the agent must get its plan approved first).
- **Pause / Resume the queue** — pauses dispatch. Running tasks finish; nothing
  new starts. Useful before a restart or when a vendor is having an outage.
- **System LLM: Vendor | Model | Billing** — the small, cheap model DevServer
  uses for its own work: Fill Task, DevPlan, graph decompose/rollup, context
  compaction, and the Ask Agent panel. Defaults to GLM. Billing works the same
  way as for tasks: *Max* runs the vendor's CLI under your subscription login,
  *API Platform* uses the API key. GLM always uses its key.

## Environment (`.env`) editor

Edits the worker's `.env` in place. The keys you are most likely to want:

| Key | For |
|---|---|
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `GLM_API_KEY` | Vendor API keys (only needed in API billing mode) |
| `GITEA_TOKEN`, `GITHUB_TOKEN` | Global git host tokens; a per-repo token overrides them |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Telegram notifications and two-way commands |
| `DISCORD_WEBHOOK_URL` | Discord notifications |
| `GIT_USER_EMAIL`, `GIT_USER_NAME` | The identity agent commits are authored with |
| `PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD`, `PGDATABASE` | Database connection |
| `OBSIDIAN_FOLDER` | Save generated plans into an Obsidian vault |
| `PREFLIGHT_IGNORE_PATTERNS` | Globs to skip during PR preflight size/secret checks |

Changing `.env` requires a worker restart to take effect.

## Notifications

Every task lifecycle event fans out to whichever channels are configured —
Telegram and Discord today, both opt-in purely by setting their env vars. You
get task start, success, failure, vendor failover, budget and
preflight alerts.

Telegram is also two-way. `/status`, `/approve KEY`, `/reject KEY`,
`/cancel KEY`, `/retry KEY`, `/pause`, `/resume`, `/mode`, `/help`,
`/reply KEY <message>`, `/inbox`, `/digest`, `/budget KEY`.

## First-time setup

`/setup` is a wizard that checks the database connection, runs migrations, and
writes the initial `.env`. It renders without the usual sidebar and header. Once
complete it sets a cookie; until then every page redirects to it.

## Backup and restore

Two scripts, both supporting local and Docker deployments:

- `scripts/devserver-backup.sh` — database dump, settings export and `.env`
  into a timestamped archive under `backups/`.
- `scripts/devserver-restore.sh` — restores an archive, snapshotting the
  existing database first.
