# Bulk Reservations API

A FastAPI service that accepts batches of up to 1000 reservations, answers `202 Accepted` right away,
and books them in background workers. It never creates duplicate bookings and never overbooks, and it applies three
levels of rate limiting. It uses Postgres and PgQueuer, and nothing else.

📄 **The task, design decisions, tests and measured results are in [reservations-task.md](reservations-task.md).**

## Quick start

```bash
cd apps/reservations
docker compose up -d        # Postgres 17 on localhost:5440, schema applied on first start
uv sync

uv run uvicorn reservations.api.app:app --port 8000     # API
uv run python -m reservations.worker.main               # worker (run as many as you like)
```

## Checks

```bash
uv run pytest                          # 34 tests against a real Postgres (uses the reservations_test database)
uv run python scripts/load_smoke.py    # 3 clients × 1000 items, spawns 1 API + 2 workers
uv run ruff check . && uv run pyright
```

## Layout

```
src/reservations/
  api/            FastAPI app, routes, problem+json errors, request-context middleware
  worker/         PgQueuer worker: chunk processing, webhooks, crash recovery
  booking.py      availability check + insert (property row locks)
  batch_state.py  counters and the single transition to a final status
  chunking.py     split items into jobs without splitting groups
  idempotency.py  Idempotency-Key + request hash
  ratelimit.py    per-client fixed-window counters
migrations/       schema (domain tables + PgQueuer 0.x tables)
tests/            pytest suite
scripts/          load test
```

This is a self-contained interview task. It shares nothing with the rest of DevServer.
