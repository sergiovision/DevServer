"""Group 9: level-1 rate limits (Postgres fixed windows)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from helpers import create_property, make_items, submit


async def _avoid_minute_boundary() -> None:
    now = datetime.now(UTC)
    if now.second >= 50:
        await asyncio.sleep(61 - now.second)


async def test_eleventh_batch_in_a_minute_is_429(api, db):
    client = await api()
    pid = await create_property(client, "client-a", 100)
    await _avoid_minute_boundary()
    first_items = make_items(1, [pid], prefix="b0")
    for n in range(10):
        resp = await submit(client, "client-a", make_items(1, [pid], prefix=f"b{n}"), key=f"k{n}")
        assert resp.status_code == 202, (n, resp.text)

    # An idempotent replay is not counted ...
    replay = await submit(client, "client-a", first_items, key="k0")
    assert replay.status_code == 200

    # ... but a new batch is.
    resp = await submit(client, "client-a", make_items(1, [pid], prefix="b10"), key="k10")
    assert resp.status_code == 429
    assert resp.headers["content-type"].startswith("application/problem+json")
    retry_after = int(resp.headers["Retry-After"])
    assert 1 <= retry_after <= 60
    assert resp.headers["RateLimit-Limit"] == "10"
    assert resp.headers["RateLimit-Remaining"] == "0"
    assert resp.headers["RateLimit-Reset"] == str(retry_after)
    assert await db.fetchval("SELECT count(*) FROM reservation_batches") == 10
    # The rejected request rolled its increment back.
    assert (
        await db.fetchval(
            "SELECT count FROM rate_limit_counters WHERE bucket = 'batches_per_minute'"
        )
        == 10
    )

    # Other clients have their own budget.
    other = await create_property(client, "client-b", 10)
    assert (await submit(client, "client-b", make_items(1, [other]))).status_code == 202


async def test_daily_item_quota(api, db):
    client = await api(items_per_day_per_client=30)
    pid = await create_property(client, "client-a", 100)
    first = make_items(20, [pid], prefix="a")
    assert (await submit(client, "client-a", first, key="a")).status_code == 202

    over = await submit(client, "client-a", make_items(11, [pid], prefix="b"), key="b")
    assert over.status_code == 429
    assert int(over.headers["Retry-After"]) >= 1
    assert over.headers["RateLimit-Remaining"] == "10"

    # Replays do not consume the quota; the whole rejected batch was rolled back.
    assert (await submit(client, "client-a", first, key="a")).status_code == 200
    assert (
        await submit(client, "client-a", make_items(10, [pid], prefix="c"), key="c")
    ).status_code == 202
    assert (
        await db.fetchval("SELECT count FROM rate_limit_counters WHERE bucket = 'items_per_day'")
        == 30
    )
    assert (
        await db.fetchval(
            "SELECT sum(count) FROM rate_limit_counters WHERE bucket = 'batches_per_minute'"
        )
        == 2
    )
