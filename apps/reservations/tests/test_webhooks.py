"""Group 11: signed webhook with retries."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from helpers import create_property, make_items, submit, wait_final
from reservations.worker.webhooks import sign


class _Recorder:
    def __init__(self, statuses: list[int]) -> None:
        self.statuses = statuses
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()


def _start_server(recorder: _Recorder) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"])).decode()
            with recorder.lock:
                idx = len(recorder.requests)
                recorder.requests.append({"headers": dict(self.headers), "body": body})
                status = recorder.statuses[min(idx, len(recorder.statuses) - 1)]
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_signature_format():
    expected = hmac.new(b"s", b"123.{}", hashlib.sha256).hexdigest()
    assert sign("s", "123", "{}") == f"sha256={expected}"


async def test_webhook_retries_until_delivered(api, workers, db):
    recorder = _Recorder([500, 500, 200])
    server = _start_server(recorder)
    try:
        client = await api()
        await workers(webhook_backoff_s="0.3,0.3")
        pid = await create_property(client, "client-a", 10)
        url = f"http://127.0.0.1:{server.server_port}/hook"
        resp = await submit(client, "client-a", make_items(2, [pid]), callback_url=url)
        batch_id = resp.json()["batch_id"]
        batch = await wait_final(client, "client-a", batch_id)
        assert batch["status"] == "completed"

        for _ in range(100):
            if await db.fetchval(
                "SELECT count(*) FROM webhook_deliveries WHERE success AND batch_id = $1::uuid",
                batch_id,
            ):
                break
            await asyncio.sleep(0.1)
        await asyncio.sleep(1)  # make sure no extra attempt follows

        assert len(recorder.requests) == 3
        rows = await db.fetch(
            "SELECT attempt, status_code, success FROM webhook_deliveries ORDER BY id"
        )
        assert [(r["attempt"], r["status_code"], r["success"]) for r in rows] == [
            (1, 500, False),
            (2, 500, False),
            (3, 200, True),
        ]

        last = recorder.requests[-1]
        h = {k.lower(): v for k, v in last["headers"].items()}
        assert h["x-batch-id"] == batch_id
        expected = hmac.new(
            b"test-secret", f"{h['x-timestamp']}.{last['body']}".encode(), hashlib.sha256
        ).hexdigest()
        assert h["x-signature"] == f"sha256={expected}"
        payload = json.loads(last["body"])
        assert payload["batch_id"] == batch_id
        assert payload["status"] == "completed"
        assert payload["succeeded"] == 2

        # Webhook trouble never changes the batch.
        after = await wait_final(client, "client-a", batch_id)
        assert after["status"] == "completed"
    finally:
        server.shutdown()


async def test_webhook_gives_up_after_max_attempts(api, workers, db):
    recorder = _Recorder([503])
    server = _start_server(recorder)
    try:
        client = await api()
        await workers(webhook_backoff_s="0.2", webhook_max_attempts=3)
        pid = await create_property(client, "client-a", 10)
        url = f"http://127.0.0.1:{server.server_port}/hook"
        resp = await submit(client, "client-a", make_items(1, [pid]), callback_url=url)
        await wait_final(client, "client-a", resp.json()["batch_id"])
        for _ in range(100):
            if await db.fetchval("SELECT count(*) FROM webhook_deliveries") >= 3:
                break
            await asyncio.sleep(0.1)
        await asyncio.sleep(1.5)
        assert len(recorder.requests) == 3
        assert not await db.fetchval("SELECT bool_or(success) FROM webhook_deliveries")
    finally:
        server.shutdown()
