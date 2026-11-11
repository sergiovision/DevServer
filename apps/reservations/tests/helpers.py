"""Shared test helpers (imported by conftest and the test modules)."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from datetime import date, timedelta
from typing import Any

import httpx

from reservations.config import Settings

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://reservations:reservations@localhost:5440/reservations_test",
)
FINAL = {"completed", "completed_with_errors", "cancelled"}


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "database_url": TEST_DATABASE_URL,
        "db_pool_max_size": 20,
        "client_busy_delay_s": 0.3,
        "item_retry_base_s": 0.01,
        "webhook_backoff_s": "0.3",
        "webhook_secret": "test-secret",
        "log_level": "WARNING",
    }
    base.update(overrides)
    return Settings(**base)


def headers(client_id: str, key: str | None = None) -> dict[str, str]:
    h = {"X-Client-Id": client_id}
    if key is not None:
        h["Idempotency-Key"] = key
    return h


async def create_property(
    client: httpx.AsyncClient, client_id: str, capacity: int, name: str = "Hotel"
) -> str:
    resp = await client.post(
        "/v1/properties", json={"name": name, "capacity": capacity}, headers=headers(client_id)
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def make_items(
    n: int,
    property_ids: list[str],
    *,
    prefix: str = "ref",
    check_in: date = date(2027, 3, 1),
    nights: int = 2,
    group_id: str | None = None,
) -> list[dict[str, Any]]:
    return [
        {
            "client_reference": f"{prefix}-{i}",
            "property_id": property_ids[i % len(property_ids)],
            "guest_name": f"Guest {i}",
            "guests": 2,
            "check_in": check_in.isoformat(),
            "check_out": (check_in + timedelta(days=nights)).isoformat(),
            **({"group_id": group_id} if group_id else {}),
        }
        for i in range(n)
    ]


async def submit(
    client: httpx.AsyncClient,
    client_id: str,
    items: list[dict[str, Any]],
    *,
    key: str | None = None,
    callback_url: str | None = None,
) -> httpx.Response:
    body: dict[str, Any] = {"items": items}
    if callback_url:
        body["callback_url"] = callback_url
    return await client.post(
        "/v1/reservation-batches", json=body, headers=headers(client_id, key or uuid.uuid4().hex)
    )


async def wait_final(
    client: httpx.AsyncClient, client_id: str, batch_id: str, timeout: float = 90
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        resp = await client.get(f"/v1/reservation-batches/{batch_id}", headers=headers(client_id))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        if body["status"] in FINAL:
            return body
        if time.monotonic() > deadline:
            raise AssertionError(f"batch {batch_id} not final after {timeout}s: {body}")
        await asyncio.sleep(0.2)


async def all_items(
    client: httpx.AsyncClient, client_id: str, batch_id: str
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 500}
        if cursor:
            params["cursor"] = cursor
        resp = await client.get(
            f"/v1/reservation-batches/{batch_id}/items", params=params, headers=headers(client_id)
        )
        assert resp.status_code == 200, resp.text
        page = resp.json()
        out.extend(page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            return out
