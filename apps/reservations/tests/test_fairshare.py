"""Group 8: per-client fair share (advisory-lock slots)."""

from __future__ import annotations

import asyncio
from datetime import datetime

import asyncpg

from helpers import TEST_DATABASE_URL, create_property, make_items, submit, wait_final
from reservations.worker.process_chunk import release_slot, try_acquire_slot


async def test_small_client_is_not_starved(api, workers, db):
    client = await api()
    pa = await create_property(client, "client-a", 1000)
    pb = await create_property(client, "client-b", 1000)

    a = await submit(client, "client-a", make_items(1000, [pa], prefix="a"))
    b = await submit(client, "client-b", make_items(25, [pb], prefix="b"))
    assert a.status_code == b.status_code == 202

    await asyncio.gather(workers(), workers())
    batch_b = await wait_final(client, "client-b", b.json()["batch_id"], timeout=120)
    batch_a = await wait_final(client, "client-a", a.json()["batch_id"], timeout=180)
    assert batch_a["status"] == batch_b["status"] == "completed"
    assert datetime.fromisoformat(batch_b["finished_at"]) < datetime.fromisoformat(
        batch_a["finished_at"]
    )


async def test_client_slots_are_exclusive(db):
    """Only CLIENT_MAX_CONCURRENT_CHUNKS slot locks can be held per client."""
    conns = [await asyncpg.connect(TEST_DATABASE_URL) for _ in range(3)]
    try:
        assert await try_acquire_slot(conns[0], "client-a", 2) == 0
        assert await try_acquire_slot(conns[1], "client-a", 2) == 1
        assert await try_acquire_slot(conns[2], "client-a", 2) is None
        assert await try_acquire_slot(conns[2], "client-b", 2) == 0  # other client unaffected
        await release_slot(conns[0], "client-a", 0)
        assert await try_acquire_slot(conns[0], "client-a", 2) == 0
    finally:
        for c in conns:
            await c.close()
