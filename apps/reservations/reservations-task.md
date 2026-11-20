# Bulk Reservations API: interview task

> **Task.** An enterprise client of a travel platform wants to create a large number of
> reservations at once (typically batches of 500, sometimes more than 1000). Build an API
> function in FastAPI that handles this reliably: throttling, rate limiting, partial failures,
> retries, and no duplicate or overbooked reservations.

This document covers the problem, the design decisions, the API, how it was tested, and the
measured results. For a quick start, see [README.md](README.md).

---

## 1. Scope

| In scope | Out of scope (deliberately) |
|---|---|
| `Property` (bookable units = `capacity`) and `Reservation` (dates, guests) | Authentication. The caller is identified by `X-Client-Id` (internal service) |
| Async bulk submit of up to 1000 items per request | External suppliers (GDS, channel managers): bookings are local |
| Idempotent submits, no duplicate bookings, no overbooking | Payments, PCI/PII handling |
| Three levels of throttling / rate limiting | Changing or cancelling a confirmed reservation |
| Per-item results, atomic groups, cancel, signed webhooks | Streaming or file upload for batches larger than 1000 items |
| Crash-safe background processing | Kubernetes, metrics stack, Redis |

## 2. Architecture

```
            POST /v1/reservation-batches                         ┌──────────── worker × N ────────────┐
client ───► FastAPI ── 1 transaction ──► Postgres ◄── dequeue ── │ fair-share slot (advisory lock)    │
   ▲          │  validate · rate-limit       │ domain tables     │ per item/group txn:                │
   │          │  batch + items + jobs        │ pgqueuer tables   │   lock items → lock properties     │
   │          └─► 202 Accepted + Location    │                   │   overlap check → insert           │
   │                                         │                   │   item status + batch counters     │
   └── GET status / items (poll) ────────────┘                   │ final status → webhook job         │
   └── signed webhook (push) ◄───────────────────────────────────┴────────────────────────────────────┘
```

* **Stack:** Python 3.14, FastAPI, asyncpg (raw SQL, because the write paths depend on explicit row locks),
  PgQueuer 0.26 (a job queue stored in Postgres), and Postgres 17.
* **API and workers are separate processes.** You can run any number of workers.
* **Postgres is the only infrastructure.** The queue lives in the same database, so a batch
  and its jobs are committed in **one transaction**. A batch can never exist without its jobs,
  and a job can never exist without its batch.

## 3. Design decisions

| # | Decision | Choice | Why |
|---|---|---|---|
| 1 | Sync vs async | **Async.** `202 Accepted` + `batch_id` + `Location`; work happens in workers | 1000 bookings take seconds. Holding the HTTP connection open breaks on load-balancer timeouts and triggers client retry storms |
| 2 | Partial failure | **Per-item results**, plus optional `group_id` for **all-or-nothing groups** (e.g. a family's rooms) | All-or-nothing over 1000 items is rarely what the business wants. Groups cover the case where it is |
| 3 | Idempotency | `Idempotency-Key` header + sha256 of the canonical body. Same key + same body returns `200` with the original batch. Same key + different body returns `409` | Clients retry after timeouts, and a retry must never create a second batch |
| 4 | Duplicate bookings | `UNIQUE (client_id, client_reference)` on reservations. A reused reference gives `DUPLICATE_CLIENT_REFERENCE` with the existing id | This also protects against the same item appearing in two different batches |
| 5 | Batch size | 1–1000 items and ≤ 5 MB, otherwise `413` with "split into several batches" | Keeps validation and memory bounded. Clients chunk larger jobs |
| 6 | Validation | Synchronous and all at once, before anything is stored: schema, dates, duplicate refs in the batch, group size → `422` with per-item paths | The client gets fast feedback and a batch is never half-accepted |
| 7 | Queue | **PgQueuer** on the same Postgres, one job per chunk of 25 items (groups are never split) | Atomic enqueue with the batch and no extra infrastructure. Chunks bound the cost of a retry |
| 8 | Rate limit, level 1 (API) | Per client: 10 batches/min, 20 000 items/day. Fixed-window counters updated inside the batch transaction → `429` + `Retry-After` + `RateLimit-*` | A rejected batch rolls back its own counter increment, so it uses no quota. Replays don't count |
| 9 | Throttle, level 2 (fairness) | At most 2 chunks per client run at once across *all* workers (Postgres advisory-lock slots). If no slot is free, the job is **deferred, not failed** | One client's 1000-item batch can't starve another client's 25 items |
| 10 | Throttle, level 3 (property) | Booking locks the property rows `FOR UPDATE` in ascending id order | Serializes bookings per property, which is what makes the overlap check correct. The fixed order prevents deadlocks |
| 11 | Retries | Transient DB errors (deadlock, serialization, connection) are retried 3× with exponential backoff and jitter. Business errors are never retried | Retrying `NO_AVAILABILITY` cannot help |
| 12 | Crash safety | PgQueuer `retry_timer` re-runs jobs from dead workers, and a periodic sweep re-queues items stuck in `processing` | Every unit re-checks the item status under `FOR UPDATE`, so a re-run never books twice |
| 13 | Result delivery | Polling (`GET` batch / paginated items) **and** an optional webhook signed with HMAC-SHA256 and retried with backoff (5 s → 30 min, 5 attempts) | Push is convenient, and polling always works even if the client's endpoint is down |
| 14 | Batch completion | Counters are updated in the same transaction as the items. A conditional `UPDATE … WHERE status NOT IN (final)` moves the batch to its final status | Exactly one transaction completes a batch, so exactly one webhook is sent |
| 15 | Multi-tenancy | Every table has `client_id` and every query filters on it. Other clients' resources return `404` | Isolation without auth, ready for real tenants |

### How one item is booked (one transaction per item or group)

1. `SELECT … FOR UPDATE` the item rows. Skip any item that is no longer `pending`/`processing`. This makes re-runs safe.
2. `SELECT … FOR UPDATE` the client's properties, in id order. A missing property, or one owned by another client, gives `PROPERTY_NOT_FOUND`.
3. Count overlapping confirmed reservations. If the count is ≥ `capacity`, the result is `NO_AVAILABILITY`.
4. `INSERT … ON CONFLICT (client_id, client_reference) DO NOTHING`. If nothing was inserted, the result is `DUPLICATE_CLIENT_REFERENCE`.
5. Update the item status and the batch counters in the same transaction. If this was the last item, set the final status and enqueue the webhook.

Group members are booked inside a savepoint. The first failure rolls the whole group back. The
item that failed keeps its own error code, and the other members get `GROUP_FAILED`.

## 4. API

Every `/v1` request needs `X-Client-Id` (`^[a-z0-9-]{3,64}$`). Errors use RFC 9457
`application/problem+json`, and every response carries `request_id` and `X-Request-Id`.

| Method | Path | Result |
|---|---|---|
| `POST` | `/v1/properties` | `201`: create a property (`name`, `capacity`) |
| `GET` | `/v1/properties/{id}` | the property |
| `POST` | `/v1/reservation-batches` | `202` new batch · `200` idempotent replay · `400` · `409` · `413` · `422` · `429` |
| `GET` | `/v1/reservation-batches/{id}` | status + counts (`succeeded/failed/cancelled/pending/processing`) |
| `GET` | `/v1/reservation-batches/{id}/items?status=&limit=&cursor=` | per-item results, keyset-paginated |
| `POST` | `/v1/reservation-batches/{id}/cancel` | cancels the items still `pending`; safe to call again |
| `GET` | `/v1/reservations/{id}` | a reservation |
| `GET` | `/healthz`, `/readyz` | liveness / database readiness |

**Batch status:** `accepted → processing → completed | completed_with_errors | cancelled`.
**Item error codes:** `PROPERTY_NOT_FOUND`, `NO_AVAILABILITY`, `DUPLICATE_CLIENT_REFERENCE`,
`GROUP_FAILED`, `INTERNAL_RETRY_EXHAUSTED`, `INTERNAL_ERROR`.

<details>
<summary>Example session (curl)</summary>

```bash
API=http://localhost:8000; C='X-Client-Id: acme-travel'

curl -s -X POST $API/v1/properties -H "$C" -H 'Content-Type: application/json' \
  -d '{"name": "Seaside Hotel", "capacity": 10}'          # -> {"id": "<PROP>", ...}

curl -si -X POST $API/v1/reservation-batches -H "$C" -H 'Idempotency-Key: order-0001' \
  -H 'Content-Type: application/json' -d '{
    "callback_url": "https://example.test/hooks/reservations",
    "items": [
      {"client_reference": "r-1", "property_id": "<PROP>", "guest_name": "Ann",
       "guests": 2, "check_in": "2027-03-01", "check_out": "2027-03-04"},
      {"client_reference": "r-2", "property_id": "<PROP>", "guest_name": "Bob",
       "guests": 1, "check_in": "2027-03-01", "check_out": "2027-03-02", "group_id": "fam"},
      {"client_reference": "r-3", "property_id": "<PROP>", "guest_name": "Cid",
       "guests": 1, "check_in": "2027-03-01", "check_out": "2027-03-02", "group_id": "fam"}
    ]}'                                                    # -> 202, Location: /v1/reservation-batches/<BATCH>

curl -s $API/v1/reservation-batches/<BATCH> -H "$C"
curl -s "$API/v1/reservation-batches/<BATCH>/items?status=failed" -H "$C"
curl -s -X POST $API/v1/reservation-batches/<BATCH>/cancel -H "$C"
```
</details>

### Webhook

When a batch has a `callback_url` and reaches a final status, the worker POSTs the batch summary with these headers:

```
X-Timestamp: 1767225600
X-Batch-Id:  <uuid>
X-Signature: sha256=<hex HMAC-SHA256(WEBHOOK_SECRET, "{X-Timestamp}.{raw body}")>
```

Any 2xx counts as delivered. Otherwise the worker retries after 5 s, 25 s, 2 min, 10 min, 30 min. Every attempt is
recorded in `webhook_deliveries`, and a partial unique index allows at most one success per batch.

## 5. Tests

There are 34 tests, all run against a **real Postgres** with real in-process workers. Nothing is mocked except the webhook receiver.

| File | What it proves |
|---|---|
| `test_validation.py` (12) | 1001 items / 0 items / > 5 MB body (with and without Content-Length) → `413`; duplicate refs, bad dates, oversized groups → `422` with item paths; missing `Idempotency-Key` / `X-Client-Id` → `400`; tenant isolation (`404`, `PROPERTY_NOT_FOUND`) |
| `test_batches.py` (6) | 1000-item happy path end to end; pagination and status filter; idempotent replay (`200`, same id, jobs enqueued once) and `409`; 5 concurrent first requests → exactly one batch; cancel |
| `test_booking.py` (6) | **No overbooking:** capacity 10, 3 × 50 overlapping items, 2 workers → exactly 10 confirmed + 140 `NO_AVAILABILITY`; group atomicity; duplicate refs across batches; re-running a chunk job never double-books; stuck items are resumed |
| `test_fairshare.py` (2) | A small client finishes while a big client's batch is still running; client slots are exclusive |
| `test_ratelimit.py` (2) | 11th batch in a minute → `429` + `Retry-After`, replay not counted; daily item quota, rejected batch stores nothing |
| `test_chunking.py` (3) | Chunking never splits a group, including when group members are not next to each other |
| `test_webhooks.py` (3) | Signature format; receiver fails twice then succeeds → delivered on attempt 3; gives up after max attempts |

Plus `scripts/load_smoke.py`: **3 clients × 1000 items** sent at the same time, 1 API + 2 worker processes,
on deliberately over-subscribed properties (10 properties × capacity 8, random 1–5 night stays over 30 days).

## 6. Results (local run: Apple Silicon, Python 3.14, Postgres 18)

```
$ uv run pytest
34 passed in 62.61s

$ uv run python scripts/load_smoke.py
load-…-0: completed_with_errors succeeded=663 failed=337
load-…-1: completed_with_errors succeeded=648 failed=352
load-…-2: completed_with_errors succeeded=667 failed=333
all 3 batches final in 28.0s
confirmed reservations: 1978
overbooked property-nights (SQL check): 0
duplicate client_reference rows: 0
PASS
```

* **Accept latency:** ~20–30 ms to validate, store and enqueue a 1000-item batch.
* **The failures are expected:** the load test asks for about 3× the available capacity, so about ⅓ of the items are `NO_AVAILABILITY`. The point is that **0 property-nights are overbooked** while the clients compete for the same properties.
* `ruff check`, `ruff format --check` and `pyright` (basic) are all clean.

### Throughput tuning (measured on an earlier prototype of the same design)

For a single client, the fair-share cap is what limits throughput, not the database:

| `CHUNK_SIZE` | `CLIENT_MAX_CONCURRENT_CHUNKS` | 1000 items, 2 workers |
|---|---|---|
| 25 | 2 | 11.5 s (87/s) |
| 25 | 8 | 1.5 s (687/s) |
| 100 | 4 | 1.4 s (737/s) |

The defaults favour fairness between clients. Raise both settings when there are only a few tenants.
One lesson from building it: deferred jobs only become visible when a worker re-checks the queue, so the worker re-checks every **1 s**
instead of PgQueuer's 30 s default. With the default, a batch stalled for tens of seconds.

## 7. Trade-offs and what I'd do next

* **Fixed-window rate limits** can let through up to 2× the limit at a window boundary. They're simple and transactional. A token bucket would fix that.
* **Bookings for one property are serialized.** That's correct, but a single very popular property becomes a hotspot. Next steps would be per-night inventory rows or `SKIP LOCKED` routing.
* **Tenant-scoped properties:** in the concurrency test, the three competing batches come from the property owner, because booking another client's property must fail.
* **Webhook URLs are not checked against SSRF.** This is an internal PoC. Add an allow-list before exposing it.
* **Production next steps:** real auth (OAuth2 client credentials → `client_id`), metrics (queue depth, chunk latency, 429 rate), autoscaling workers on queue depth, and moving the PgQueuer pin to 1.x.

## 8. Configuration

All limits are environment variables (`src/reservations/config.py`):

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `postgresql://reservations:reservations@localhost:5440/reservations` | |
| `MAX_ITEMS_PER_BATCH` / `MAX_BODY_BYTES` | 1000 / 5 MiB | over the limit → `413` |
| `BATCHES_PER_MINUTE_PER_CLIENT` / `ITEMS_PER_DAY_PER_CLIENT` | 10 / 20000 | level 1 |
| `CHUNK_SIZE` | 25 | items per queue job (groups never split) |
| `WORKER_CONCURRENCY` / `WORKER_REQUESTS_PER_SECOND` | 8 / unlimited | per worker process |
| `CLIENT_MAX_CONCURRENT_CHUNKS` / `CLIENT_BUSY_DELAY_S` | 2 / 2 | level 2 (fair share) |
| `ITEM_MAX_ATTEMPTS` / `ITEM_RETRY_BASE_S` | 3 / 0.2 | transient-error retries |
| `STUCK_ITEM_TIMEOUT_S` | 300 | crash recovery |
| `WEBHOOK_SECRET` / `WEBHOOK_MAX_ATTEMPTS` / `WEBHOOK_BACKOFF_S` | dev value / 5 / `5,25,120,600,1800` | **set the secret** |
| `LOG_LEVEL` | INFO | JSON logs with `request_id`, `client_id`, `batch_id`, `job_id` |
