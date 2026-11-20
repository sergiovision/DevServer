"""Groups 4-7: no overbooking, group atomicity, duplicates, retry idempotency."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections import Counter

from pgqueuer.models import Job
from pgqueuer.queries import Queries

from helpers import all_items, create_property, make_items, make_settings, submit, wait_final
from reservations.db import create_pool
from reservations.worker.process_chunk import WorkerDeps, process_chunk_job


async def test_no_overbooking_under_concurrency(api, workers, db):
    """Capacity 10, 3 batches x 50 identical-date items, 2 workers at once.

    Properties are tenant-scoped (a client can only book its own), so the three
    competing batches come from the property owner. The fair-share cap is raised
    so all six chunks really do run concurrently across the two workers.
    """
    client = await api()
    pid = await create_property(client, "owner", 10)
    batch_ids = []
    for b in range(3):
        resp = await submit(client, "owner", make_items(50, [pid], prefix=f"b{b}"))
        assert resp.status_code == 202
        batch_ids.append(resp.json()["batch_id"])

    await asyncio.gather(
        workers(client_max_concurrent_chunks=8), workers(client_max_concurrent_chunks=8)
    )
    codes: Counter[str | None] = Counter()
    for bid in batch_ids:
        batch = await wait_final(client, "owner", bid)
        assert batch["counts"]["pending"] == batch["counts"]["processing"] == 0
        codes.update(i["error_code"] for i in await all_items(client, "owner", bid))

    assert (
        await db.fetchval(
            "SELECT count(*) FROM reservations WHERE property_id = $1 AND status = 'confirmed'",
            uuid.UUID(pid),
        )
        == 10
    )
    assert codes == Counter({None: 10, "NO_AVAILABILITY": 140})


async def test_group_is_atomic(api, workers, db):
    client = await api()
    await workers()
    roomy = await create_property(client, "client-a", 10, "Roomy")
    tiny = await create_property(client, "client-a", 1, "Tiny")
    first = await submit(client, "client-a", make_items(1, [tiny], prefix="pre"))
    assert (await wait_final(client, "client-a", first.json()["batch_id"]))["status"] == "completed"

    group = make_items(3, [roomy], prefix="grp", group_id="family")
    group[2]["property_id"] = tiny  # same dates: tiny is already full
    resp = await submit(client, "client-a", group)
    batch = await wait_final(client, "client-a", resp.json()["batch_id"])
    assert batch["status"] == "completed_with_errors"
    assert batch["counts"]["failed"] == 3

    items = await all_items(client, "client-a", batch["batch_id"])
    assert [i["status"] for i in items] == ["failed"] * 3
    assert [i["error_code"] for i in items] == ["GROUP_FAILED", "GROUP_FAILED", "NO_AVAILABILITY"]
    assert all(i["reservation_id"] is None for i in items)
    assert (
        await db.fetchval("SELECT count(*) FROM reservations WHERE client_reference LIKE 'grp-%'")
        == 0
    )


async def test_large_group_rolls_back_completely(api, workers, db):
    """A 10-member group on a capacity-9 property: the 10th member fails, so all do."""
    client = await api()
    await workers()
    pid = await create_property(client, "client-a", 9)
    resp = await submit(client, "client-a", make_items(10, [pid], prefix="g", group_id="g1"))
    batch = await wait_final(client, "client-a", resp.json()["batch_id"])
    assert batch["counts"]["failed"] == 10
    codes = [i["error_code"] for i in await all_items(client, "client-a", batch["batch_id"])]
    assert codes == ["GROUP_FAILED"] * 9 + ["NO_AVAILABILITY"]
    assert await db.fetchval("SELECT count(*) FROM reservations") == 0


async def test_duplicate_client_reference_across_batches(api, workers):
    client = await api()
    await workers()
    pid = await create_property(client, "client-a", 10)
    first = await submit(client, "client-a", make_items(1, [pid], prefix="dup"))
    await wait_final(client, "client-a", first.json()["batch_id"])
    (original,) = await all_items(client, "client-a", first.json()["batch_id"])
    assert original["status"] == "succeeded"

    second = await submit(client, "client-a", make_items(1, [pid], prefix="dup"))
    batch = await wait_final(client, "client-a", second.json()["batch_id"])
    assert batch["status"] == "completed_with_errors"
    (item,) = await all_items(client, "client-a", second.json()["batch_id"])
    assert item["error_code"] == "DUPLICATE_CLIENT_REFERENCE"
    assert item["error_details"] == {"reservation_id": original["reservation_id"]}


async def test_rerunning_a_chunk_job_is_idempotent(api, db):
    """Simulates a crash after commit: the same job is delivered twice."""
    client = await api()
    pid = await create_property(client, "client-a", 100)
    resp = await submit(client, "client-a", make_items(25, [pid]))
    batch_id = resp.json()["batch_id"]
    row = await db.fetchrow("SELECT * FROM pgqueuer WHERE entrypoint = 'reservations-chunk'")
    job = Job.model_validate(dict(row))
    assert json.loads(job.payload or b"")["batch_id"] == batch_id

    settings = make_settings()
    pool = await create_pool(settings, max_size=5)
    deps = WorkerDeps(pool=pool, settings=settings, queries=Queries.from_asyncpg_pool(pool))
    try:
        await process_chunk_job(job, deps)
        snapshot = await db.fetchrow(
            "SELECT * FROM reservation_batches WHERE id = $1::uuid", batch_id
        )
        # Crash between commit and ack: an item left in 'processing' must not re-book.
        await process_chunk_job(job, deps)
        await process_chunk_job(job, deps)
    finally:
        await deps.http.aclose()
        await pool.close()

    assert await db.fetchval("SELECT count(*) FROM reservations") == 25
    after = await db.fetchrow("SELECT * FROM reservation_batches WHERE id = $1::uuid", batch_id)
    assert dict(after) == dict(snapshot)
    assert after["status"] == "completed"
    assert after["succeeded"] == 25
    assert (
        await db.fetchval(
            "SELECT max(attempts) FROM reservation_batch_items WHERE batch_id = $1::uuid", batch_id
        )
        == 1
    )


async def test_items_stuck_in_processing_are_resumed(api, db):
    """A chunk that crashed after marking items 'processing' is safely rerun."""
    client = await api()
    pid = await create_property(client, "client-a", 100)
    batch_id = (await submit(client, "client-a", make_items(10, [pid]))).json()["batch_id"]
    row = await db.fetchrow("SELECT * FROM pgqueuer WHERE entrypoint = 'reservations-chunk'")
    job = Job.model_validate(dict(row))
    await db.execute(
        "UPDATE reservation_batch_items SET status = 'processing' WHERE batch_id = $1::uuid",
        batch_id,
    )
    settings = make_settings()
    pool = await create_pool(settings, max_size=5)
    deps = WorkerDeps(pool=pool, settings=settings, queries=Queries.from_asyncpg_pool(pool))
    try:
        await process_chunk_job(job, deps)
    finally:
        await deps.http.aclose()
        await pool.close()
    batch = await db.fetchrow("SELECT * FROM reservation_batches WHERE id = $1::uuid", batch_id)
    assert batch["status"] == "completed"
    assert await db.fetchval("SELECT count(*) FROM reservations") == 10
