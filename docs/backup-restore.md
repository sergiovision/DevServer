# Backup & Restore

Back to the [README](../README.md).

DevServer includes scripts for full-system backup and restore. Pass the mode
first: `local` (default — host PostgreSQL via the `PG*` vars in `.env`) or
`docker` (the bundled `${DEVSERVER_STACK:-devserver}-postgres` container).

```bash
# Backup — creates a timestamped archive in backups/
bash scripts/devserver-backup.sh [local|docker] [/path/to/output.tar.gz]

# Restore from archive (prompts before dropping the database or overwriting .env)
bash scripts/devserver-restore.sh [local|docker] backups/devserver-backup-YYYYMMDD-HHMMSS.tar.gz
```

The archive contains:

| Path | Content |
|---|---|
| `db/devserver.dump` | `pg_dump --format=custom` of the whole database |
| `meta/settings.json` | Human-readable JSON snapshot of the `settings` table |
| `meta/dot-env` | The repo-root `.env` (secrets — handle the archive with care) |
| `meta/docker-dot-env` | `docker/.env`, when it is a real file (as `setup-release.sh` writes it) rather than a symlink to the root `.env` |
| `meta/backup-info.json` | Timestamp, mode, database name, hostname |

Worktrees and task logs are intentionally **not** in the archive — they are
reproducible or transient and would dominate its size. Back them up separately
if you need them (see below).

Both scripts treat their output as secret: the backup runs under `umask 077`,
the default `backups/` directory is `chmod 700` and the archive `chmod 600`;
restored `.env` files are `chmod 600`.

Restore, in order:

1. Shows the target (mode, database, host/user or container) and asks for
   confirmation. That target is frozen: the backup's `.env` is written only
   after the database is restored and is never re-read by the script, so it
   cannot redirect the restore elsewhere. On a fresh machine with no `.env`,
   export the `PG*` variables (or copy `meta/dot-env` into place) first.
2. If the database exists, snapshots it to
   `backups/pre-restore-<timestamp>.dump` (custom format, mode 600). If the
   snapshot fails, nothing is dropped and the script exits non-zero.
3. Drops and recreates the database, ensures the `vector` extension, then
   `pg_restore`s the dump as a single transaction with `--exit-on-error`.
   Any failed step exits non-zero and prints how to put the snapshot back
   (`pg_restore --clean --if-exists … backups/pre-restore-….dump`); a failed
   `pg_restore` leaves an empty database, never a half-restored one. If the
   error is harmless (typically `must be owner of extension` on a
   non-superuser role), re-run with `DEVSERVER_RESTORE_LENIENT=1`: pg_restore
   then continues past errors and the script reports them instead.
4. Offers to restore `.env` / `docker/.env` (the current files are kept as
   `.env.pre-restore`).

Afterwards:

```bash
bash scripts/migrate.sh [local|docker]   # apply any schema additions
bash scripts/start.sh [--docker]         # start services
```

## Docker named volumes

| Volume | Container path | Backup | How |
|---|---|---|---|
| `postgres_data` | `/var/lib/postgresql/data` | **required** | `pg_dump` (the backup script) — never tar the live data dir |
| `worktrees` | `/app/worktrees` | recommended | tar while the worker is stopped |
| `worker_config` | `/app/config` | recommended | tar anytime — holds the `.env` saved by the Settings/Setup Database card |
| `worker_logs` | `/app/logs` | optional | tar anytime |
| `agent_clis` | `/opt/agents` | never | vendor CLIs fetched on first use; refills itself |

Volume names are prefixed with the compose project (`devserver_` by default;
`docker volume ls` shows the real names). The hardened overlay
(`docker/docker-compose.hardened.yml`) labels each volume with this
disposition, so backups can be scripted without re-reading the compose file:

```bash
docker volume inspect devserver_postgres_data --format '{{ json .Labels }}'
```

Labels are applied when a volume is created; an existing volume keeps the
labels it was created with.

### Manual volume tar (worktrees / config / logs only)

```bash
docker run --rm \
  -v devserver_worktrees:/src:ro \
  -v "$PWD":/out \
  alpine tar czf /out/worktrees.tgz -C /src .
```

Do **not** use this pattern on `postgres_data` — a live-file tar of a running
PostgreSQL volume produces a corrupt snapshot. Always go through `pg_dump`.

## Log rotation (hardened overlay)

`docker/docker-compose.hardened.yml` caps container logs via Docker's
`json-file` driver — no host `logrotate` config needed:

- `postgres`: 10 MB × 5 files  = 50 MB
- `web`:      10 MB × 5 files  = 50 MB
- `worker`:   20 MB × 10 files = 200 MB

```bash
cd docker
docker compose -f docker-compose.yml -f docker-compose.hardened.yml up -d --build
```
