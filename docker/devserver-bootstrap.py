#!/usr/bin/env python
"""First-start bootstrap for a DevServer container.

Runs from the entrypoint before the worker's uvicorn, and exists because a
fresh deployment otherwise comes up *silently broken*:

  * The image ships ``scripts/migrate.sh`` but that script shells out to
    ``psql``, which is not installed here — and until now the SQL it wants was
    not in the image either. A new Postgres volume therefore started with an
    EMPTY schema. The worker still bound its port and reported healthy, so the
    only symptom was ``relation "schedules" does not exist`` buried in the log
    and a dashboard stuck on the setup wizard.
  * The release compose (unlike the dev one) does not mount the migrations
    into ``/docker-entrypoint-initdb.d``, so Postgres' own init hook never
    applied them either.

So the schema is applied here instead, over asyncpg, which the worker venv
already has. No psql, no extra image weight.

Everything is idempotent: it is safe on every start, not just the first. The
migration pass is skipped entirely once the schema is populated, so the cost
on a warm container is one ``count(*)`` against information_schema.

Environment:
  DATABASE_URL              required; the ``+asyncpg`` SQLAlchemy suffix is tolerated
  DEVSERVER_BOOTSTRAP       set to ``0`` to skip entirely
  DEVSERVER_MIGRATIONS_DIR  default /app/database/migrations
  DEVSERVER_DB_WAIT         seconds to wait for Postgres (default 90)
  DEVSERVER_SETTING_<KEY>   seeds settings['<key>'] (lowercased) — see _seed_settings
  DEVSERVER_SETUP_COMPLETED truthy marks setup_completed so the dashboard skips
                            the first-boot wizard when config came from env
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

try:
    import asyncpg
except ImportError:  # pragma: no cover - the venv always has it
    print("[bootstrap] asyncpg unavailable — skipping", flush=True)
    sys.exit(0)


def log(msg: str) -> None:
    print(f"[bootstrap] {msg}", flush=True)


def dsn() -> str:
    """DATABASE_URL as libpq wants it.

    The worker's copy carries SQLAlchemy's ``postgresql+asyncpg://`` dialect
    prefix; asyncpg itself rejects that.
    """
    url = os.environ.get("DATABASE_URL", "").strip()
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def wait_for_db(url: str, timeout: int) -> asyncpg.Connection | None:
    """Poll until Postgres accepts a connection, or give up after ``timeout``.

    Compose's ``depends_on: healthy`` already gates this, but that only proves
    the postmaster answers pg_isready — not that this role can authenticate.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    last = ""
    while True:
        try:
            return await asyncpg.connect(url, timeout=10)
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
            if asyncio.get_running_loop().time() >= deadline:
                log(f"database unreachable after {timeout}s — {last}")
                return None
            await asyncio.sleep(2)


async def _table_count(conn: asyncpg.Connection) -> int:
    return await conn.fetchval(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'"
    )


async def apply_migrations(conn: asyncpg.Connection, directory: Path) -> bool:
    """Apply every ``*.sql`` in filename order when the schema is empty.

    Deliberately all-or-nothing on an empty schema rather than a per-file
    ledger: the migrations are written to be idempotent (``IF NOT EXISTS`` /
    ``IF EXISTS`` guards throughout) and consolidated into 001, so tracking
    which ones "ran" would add state without adding safety. A populated schema
    is left strictly alone — this must never mutate an existing deployment.
    """
    existing = await _table_count(conn)
    if existing:
        log(f"schema already present ({existing} tables) — no migrations applied")
        return True

    if not directory.is_dir():
        log(f"NO MIGRATIONS at {directory} — schema stays empty, worker will fail")
        return False

    files = sorted(p for p in directory.glob("*.sql") if p.is_file())
    if not files:
        log(f"no .sql files in {directory} — schema stays empty")
        return False

    log(f"empty schema — applying {len(files)} migration(s) from {directory}")
    for path in files:
        sql = path.read_text(encoding="utf-8")
        try:
            # No parameters, so asyncpg uses the simple query protocol, which
            # is what lets one execute() carry a whole multi-statement file
            # (DO blocks and $$-quoted bodies included).
            await conn.execute(sql)
            log(f"  applied {path.name}")
        except Exception as exc:
            log(f"  FAILED {path.name}: {type(exc).__name__}: {exc}")
            return False

    log(f"schema ready ({await _table_count(conn)} tables)")
    return True


def _json_value(raw: str) -> str:
    """Encode an env string the way the settings table stores values.

    ``settings.value`` is JSONB — ``'"glm"'``, ``'2'``, ``'false'``. Parsing
    first keeps ``true``/``7`` correctly typed instead of stringifying them,
    and anything that is not valid JSON becomes a JSON string.
    """
    raw = raw.strip()
    try:
        json.loads(raw)
        return raw
    except ValueError:
        return json.dumps(raw)


async def seed_settings(conn: asyncpg.Connection) -> None:
    """Seed settings rows from the environment.

    Two sources: any ``DEVSERVER_SETTING_<KEY>`` var, and the
    ``DEVSERVER_SETUP_COMPLETED`` shortcut for the first-boot wizard gate.

    Insert-only (``DO NOTHING``). A container restart must not stamp on a value
    the operator later changed in the dashboard — env seeds the initial state,
    it does not own it.
    """
    pairs: list[tuple[str, str]] = []

    for name, raw in os.environ.items():
        if name.startswith("DEVSERVER_SETTING_") and raw.strip():
            key = name[len("DEVSERVER_SETTING_"):].lower()
            if key:
                pairs.append((key, _json_value(raw)))

    flag = os.environ.get("DEVSERVER_SETUP_COMPLETED", "").strip().lower()
    if flag in {"1", "true", "yes", "on"}:
        pairs.append(("setup_completed", "true"))

    if not pairs:
        return

    for key, value in pairs:
        try:
            status = await conn.execute(
                "INSERT INTO settings (key, value) VALUES ($1, $2::jsonb) "
                "ON CONFLICT (key) DO NOTHING",
                key, value,
            )
            # "INSERT 0 1" vs "INSERT 0 0" — say which actually happened rather
            # than claiming to have seeded a key that was already set.
            if status.rsplit(" ", 1)[-1] == "0":
                log(f"  setting {key} already set — left alone")
            else:
                log(f"  seeded setting {key}={value}")
        except Exception as exc:
            log(f"  could not seed {key}: {type(exc).__name__}: {exc}")


async def main() -> int:
    if os.environ.get("DEVSERVER_BOOTSTRAP", "").strip() == "0":
        log("disabled via DEVSERVER_BOOTSTRAP=0")
        return 0

    url = dsn()
    if not url:
        log("DATABASE_URL unset — skipping")
        return 0

    conn = await wait_for_db(url, int(os.environ.get("DEVSERVER_DB_WAIT", "90")))
    if conn is None:
        # Never fatal: the worker has its own retry/reporting, and a bootstrap
        # failure must not be the thing that keeps the tier from starting.
        return 0

    try:
        directory = Path(
            os.environ.get("DEVSERVER_MIGRATIONS_DIR", "/app/database/migrations")
        )
        if await apply_migrations(conn, directory):
            await seed_settings(conn)
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception as exc:  # pragma: no cover - belt and braces
        log(f"unexpected error, continuing to start: {type(exc).__name__}: {exc}")
        sys.exit(0)
