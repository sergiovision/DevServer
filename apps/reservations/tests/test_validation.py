"""Groups 2 and 12: synchronous validation, X-Client-Id, tenant isolation."""

from __future__ import annotations

import json
import uuid

from helpers import all_items, create_property, headers, make_items, submit, wait_final


async def test_too_many_items_is_413(api):
    client = await api()
    pid = await create_property(client, "client-a", 10)
    resp = await submit(client, "client-a", make_items(1001, [pid]))
    assert resp.status_code == 413
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert "split" in resp.json()["detail"]


async def test_empty_batch_is_413(api):
    client = await api()
    resp = await submit(client, "client-a", [])
    assert resp.status_code == 413


async def test_body_over_limit_is_413_with_content_length(api):
    client = await api()
    body = json.dumps({"items": [], "pad": "x" * (6 * 1024 * 1024)}).encode()
    resp = await client.post(
        "/v1/reservation-batches",
        content=body,
        headers={**headers("client-a", "k1"), "Content-Type": "application/json"},
    )
    assert resp.status_code == 413
    assert resp.json()["request_id"]


async def test_body_over_limit_is_413_when_streamed(api):
    """No Content-Length: the middleware counts streamed bytes."""
    client = await api()

    async def stream():
        yield b'{"items": [], "pad": "'
        for _ in range(6):
            yield b"x" * (1024 * 1024)
        yield b'"}'

    resp = await client.post(
        "/v1/reservation-batches",
        content=stream(),
        headers={**headers("client-a", "k1"), "Content-Type": "application/json"},
    )
    assert "content-length" not in {k.lower() for k in resp.request.headers}
    assert resp.status_code == 413


async def test_duplicate_client_reference_is_422(api):
    client = await api()
    pid = await create_property(client, "client-a", 10)
    items = make_items(3, [pid])
    items[2]["client_reference"] = items[0]["client_reference"]
    resp = await submit(client, "client-a", items)
    assert resp.status_code == 422
    errors = resp.json()["errors"]
    assert {"path": "items[2].client_reference"}.items() <= errors[0].items()


async def test_check_out_not_after_check_in_is_422(api):
    client = await api()
    pid = await create_property(client, "client-a", 10)
    items = make_items(2, [pid])
    items[1]["check_out"] = items[1]["check_in"]
    resp = await submit(client, "client-a", items)
    assert resp.status_code == 422
    assert [e["path"] for e in resp.json()["errors"]] == ["items[1]"]


async def test_field_errors_have_item_paths(api):
    client = await api()
    pid = await create_property(client, "client-a", 10)
    items = make_items(2, [pid])
    items[1]["guests"] = 0
    resp = await submit(client, "client-a", items)
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["path"] == "items[1].guests"


async def test_group_larger_than_chunk_is_422(api):
    client = await api()
    pid = await create_property(client, "client-a", 100)
    resp = await submit(client, "client-a", make_items(26, [pid], group_id="g"))
    assert resp.status_code == 422


async def test_missing_idempotency_key_is_400(api):
    client = await api()
    pid = await create_property(client, "client-a", 10)
    resp = await client.post(
        "/v1/reservation-batches",
        json={"items": make_items(1, [pid])},
        headers=headers("client-a"),
    )
    assert resp.status_code == 400


async def test_missing_or_invalid_client_id_is_400(api):
    client = await api()
    resp = await client.get(f"/v1/reservation-batches/{uuid.uuid4()}")
    assert resp.status_code == 400
    assert resp.json()["request_id"]
    resp = await client.post("/v1/properties", json={"name": "x", "capacity": 1})
    assert resp.status_code == 400
    resp = await client.get(f"/v1/reservation-batches/{uuid.uuid4()}", headers=headers("AB"))
    assert resp.status_code == 400
    # Health endpoints do not need it.
    assert (await client.get("/healthz")).status_code == 200
    assert (await client.get("/readyz")).status_code == 200


async def test_other_client_cannot_read_batch(api):
    client = await api()
    pid = await create_property(client, "client-a", 10)
    resp = await submit(client, "client-a", make_items(2, [pid]))
    assert resp.status_code == 202
    batch_id = resp.json()["batch_id"]
    base = f"/v1/reservation-batches/{batch_id}"
    assert (await client.get(base, headers=headers("client-a"))).status_code == 200
    assert (await client.get(base, headers=headers("client-b"))).status_code == 404
    assert (await client.get(f"{base}/items", headers=headers("client-b"))).status_code == 404
    assert (await client.post(f"{base}/cancel", headers=headers("client-b"))).status_code == 404
    assert (
        await client.get(f"/v1/properties/{pid}", headers=headers("client-b"))
    ).status_code == 404


async def test_booking_another_clients_property_fails(api, workers):
    client = await api()
    await workers()
    pid = await create_property(client, "client-a", 10)
    resp = await submit(client, "client-b", make_items(1, [pid]))
    batch = await wait_final(client, "client-b", resp.json()["batch_id"])
    assert batch["status"] == "completed_with_errors"
    (item,) = await all_items(client, "client-b", batch["batch_id"])
    assert item["error_code"] == "PROPERTY_NOT_FOUND"
