"""Groups 1, 3 and 10: happy path, idempotency, cancel."""

from __future__ import annotations

import asyncio
import uuid

from helpers import all_items, create_property, headers, make_items, submit, wait_final


async def test_happy_path_1000_items(api, workers, db):
    client = await api()
    await workers()
    await workers()
    pids = [await create_property(client, "client-a", 1000, f"P{i}") for i in range(5)]

    resp = await submit(client, "client-a", make_items(1000, pids))
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["total_items"] == 1000
    assert resp.headers["location"] == body["links"]["self"]
    assert body["links"]["items"].endswith("/items")
    assert body["request_id"]
    batch = await wait_final(client, "client-a", body["batch_id"], timeout=120)
    assert batch["status"] == "completed"
    assert batch["counts"] == {
        "succeeded": 1000,
        "failed": 0,
        "cancelled": 0,
        "pending": 0,
        "processing": 0,
    }
    assert batch["started_at"] and batch["finished_at"]
    assert await db.fetchval("SELECT count(*) FROM reservations") == 1000

    items = await all_items(client, "client-a", body["batch_id"])
    assert len(items) == 1000
    assert [i["seq"] for i in items] == list(range(1000))
    assert all(i["status"] == "succeeded" and i["reservation_id"] for i in items)

    rid = items[0]["reservation_id"]
    resp = await client.get(f"/v1/reservations/{rid}", headers=headers("client-a"))
    assert resp.status_code == 200
    assert resp.json()["client_reference"] == "ref-0"
    resp = await client.get(f"/v1/reservations/{rid}", headers=headers("client-b"))
    assert resp.status_code == 404


async def test_items_pagination_and_status_filter(api, db):
    client = await api()
    pid = await create_property(client, "client-a", 10)
    batch_id = (await submit(client, "client-a", make_items(7, [pid]))).json()["batch_id"]
    url = f"/v1/reservation-batches/{batch_id}/items"
    page1 = (await client.get(url, params={"limit": 5}, headers=headers("client-a"))).json()
    assert [i["seq"] for i in page1["items"]] == [0, 1, 2, 3, 4]
    page2 = (
        await client.get(
            url, params={"limit": 5, "cursor": page1["next_cursor"]}, headers=headers("client-a")
        )
    ).json()
    assert [i["seq"] for i in page2["items"]] == [5, 6]
    assert page2["next_cursor"] is None
    pending = (
        await client.get(url, params={"status": "succeeded"}, headers=headers("client-a"))
    ).json()
    assert pending["items"] == []
    assert (
        await client.get(url, params={"limit": 501}, headers=headers("client-a"))
    ).status_code == 422


async def test_idempotent_replay(api, db):
    client = await api()
    pid = await create_property(client, "client-a", 10)
    items = make_items(30, [pid])

    first = await submit(client, "client-a", items, key="key-1")
    assert first.status_code == 202
    jobs_after_first = await db.fetchval("SELECT count(*) FROM pgqueuer")
    assert jobs_after_first == 2  # 30 items -> chunks of 25 + 5

    # Same body with different key order / whitespace still hashes the same.
    second = await submit(
        client, "client-a", [dict(reversed(i.items())) for i in items], key="key-1"
    )
    assert second.status_code == 200
    assert second.json()["batch_id"] == first.json()["batch_id"]
    assert await db.fetchval("SELECT count(*) FROM reservation_batches") == 1
    assert await db.fetchval("SELECT count(*) FROM pgqueuer") == jobs_after_first

    other = await submit(client, "client-a", items[:5], key="key-1")
    assert other.status_code == 409
    assert other.headers["content-type"].startswith("application/problem+json")

    # The same key under another client is a different namespace.
    other_client = await submit(client, "client-b", items, key="key-1")
    assert other_client.status_code == 202


async def test_concurrent_first_requests_create_one_batch(api, db):
    client = await api()
    pid = await create_property(client, "client-a", 10)
    items = make_items(10, [pid])
    key = uuid.uuid4().hex
    responses = await asyncio.gather(
        *(submit(client, "client-a", items, key=key) for _ in range(5))
    )
    codes = sorted(r.status_code for r in responses)
    assert codes == [200, 200, 200, 200, 202]
    assert len({r.json()["batch_id"] for r in responses}) == 1
    assert await db.fetchval("SELECT count(*) FROM reservation_batches") == 1
    assert await db.fetchval("SELECT count(*) FROM reservation_batch_items") == 10
    assert await db.fetchval("SELECT count(*) FROM pgqueuer") == 1
    # Only the winner consumed rate-limit budget.
    assert (
        await db.fetchval(
            "SELECT count FROM rate_limit_counters WHERE bucket = 'batches_per_minute'"
        )
        == 1
    )


async def test_cancel_right_after_submit(api, workers, db):
    client = await api()
    pid = await create_property(client, "client-a", 100)
    resp = await submit(client, "client-a", make_items(60, [pid]))
    batch_id = resp.json()["batch_id"]

    cancel = await client.post(
        f"/v1/reservation-batches/{batch_id}/cancel", headers=headers("client-a")
    )
    assert cancel.status_code == 200
    body = cancel.json()
    assert body["status"] == "cancelled"
    assert body["counts"]["cancelled"] == 60
    assert body["finished_at"]

    again = await client.post(
        f"/v1/reservation-batches/{batch_id}/cancel", headers=headers("client-a")
    )
    assert again.status_code == 200
    assert again.json()["counts"] == body["counts"]
    assert again.json()["finished_at"] == body["finished_at"]

    # The already-enqueued chunk jobs run and book nothing.
    await workers()
    for _ in range(100):
        if await db.fetchval("SELECT count(*) FROM pgqueuer") == 0:
            break
        await asyncio.sleep(0.1)
    assert await db.fetchval("SELECT count(*) FROM reservations") == 0
    items = await all_items(client, "client-a", batch_id)
    assert {i["status"] for i in items} == {"cancelled"}
    final = await wait_final(client, "client-a", batch_id)
    assert final["status"] == "cancelled"


async def test_cancel_does_not_touch_finished_items(api, workers, db):
    client = await api()
    await workers()
    pid = await create_property(client, "client-a", 100)
    batch_id = (await submit(client, "client-a", make_items(5, [pid]))).json()["batch_id"]
    done = await wait_final(client, "client-a", batch_id)
    assert done["status"] == "completed"
    resp = await client.post(
        f"/v1/reservation-batches/{batch_id}/cancel", headers=headers("client-a")
    )
    assert resp.json()["status"] == "completed"
    assert resp.json()["counts"]["succeeded"] == 5
