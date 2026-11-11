"""Load smoke test: 3 clients x 1000 items, 1 API + 2 workers, all local.

    uv run python scripts/load_smoke.py            # spawns API + 2 workers
    uv run python scripts/load_smoke.py --no-spawn # use already running processes

Passes when every batch reaches a final status within --deadline seconds
(default 120), the SQL overbooking check finds no property-night above
capacity, and there are no duplicate (client_id, client_reference) pairs.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import subprocess
import sys
import time
import uuid
from datetime import date, timedelta
from typing import Any

import asyncpg
import httpx

FINAL = {"completed", "completed_with_errors", "cancelled"}

OVERBOOKING_SQL = """
SELECT r.property_id, d::date AS night, count(*) AS booked, p.capacity
  FROM reservations r
  JOIN properties p ON p.id = r.property_id
 CROSS JOIN LATERAL generate_series(r.check_in, r.check_out - 1, interval '1 day') AS d
 WHERE r.client_id = ANY($1::text[]) AND r.status = 'confirmed'
 GROUP BY r.property_id, d::date, p.capacity
HAVING count(*) > p.capacity
"""

DUPLICATES_SQL = """
SELECT client_id, client_reference, count(*) FROM reservations
 WHERE client_id = ANY($1::text[])
 GROUP BY 1, 2 HAVING count(*) > 1
"""


def _spawn(n_workers: int, port: int) -> list[subprocess.Popen[bytes]]:
    env = {**os.environ, "LOG_LEVEL": os.environ.get("LOG_LEVEL", "WARNING")}
    procs = [
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "reservations.api.app:app",
                "--port",
                str(port),
                "--log-level",
                "warning",
            ],
            env=env,
        )
    ]
    procs += [
        subprocess.Popen([sys.executable, "-m", "reservations.worker.main"], env=env)
        for _ in range(n_workers)
    ]
    return procs


async def _wait_ready(http: httpx.AsyncClient, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if (await http.get("/readyz")).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.3)
    raise SystemExit("API did not become ready")


async def _run_client(
    http: httpx.AsyncClient, client_id: str, n_items: int, rng: random.Random
) -> dict[str, Any]:
    h = {"X-Client-Id": client_id}
    props = []
    for i in range(10):
        resp = await http.post("/v1/properties", json={"name": f"P{i}", "capacity": 8}, headers=h)
        resp.raise_for_status()
        props.append(resp.json()["id"])
    base = date(2027, 4, 1)
    items = []
    for i in range(n_items):
        check_in = base + timedelta(days=rng.randrange(30))
        items.append(
            {
                "client_reference": f"{client_id}-{i}",
                "property_id": rng.choice(props),
                "guest_name": f"Guest {i}",
                "guests": rng.randint(1, 4),
                "check_in": check_in.isoformat(),
                "check_out": (check_in + timedelta(days=rng.randint(1, 5))).isoformat(),
            }
        )
    resp = await http.post(
        "/v1/reservation-batches",
        json={"items": items},
        headers={**h, "Idempotency-Key": uuid.uuid4().hex},
    )
    if resp.status_code != 202:
        raise SystemExit(f"{client_id}: submit failed {resp.status_code} {resp.text}")
    batch_id = resp.json()["batch_id"]
    while True:
        batch = (await http.get(f"/v1/reservation-batches/{batch_id}", headers=h)).json()
        if batch["status"] in FINAL:
            return batch
        await asyncio.sleep(0.5)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--database-url",
        default=os.environ.get(
            "DATABASE_URL", "postgresql://reservations:reservations@localhost:5440/reservations"
        ),
    )
    parser.add_argument("--clients", type=int, default=3)
    parser.add_argument("--items", type=int, default=1000)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--deadline", type=float, default=120)
    parser.add_argument("--no-spawn", action="store_true")
    args = parser.parse_args()

    procs: list[subprocess.Popen[bytes]] = []
    if not args.no_spawn:
        os.environ.setdefault("DATABASE_URL", args.database_url)
        procs = _spawn(args.workers, int(httpx.URL(args.api_url).port or 8000))
    try:
        async with httpx.AsyncClient(base_url=args.api_url, timeout=60) as http:
            await _wait_ready(http)
            run = uuid.uuid4().hex[:8]
            clients = [f"load-{run}-{i}" for i in range(args.clients)]
            rng = random.Random(run)
            started = time.monotonic()
            try:
                batches = await asyncio.wait_for(
                    asyncio.gather(*(_run_client(http, c, args.items, rng) for c in clients)),
                    timeout=args.deadline,
                )
            except TimeoutError:
                print(f"FAIL: batches not final within {args.deadline}s")
                return 1
            elapsed = time.monotonic() - started

        for c, b in zip(clients, batches, strict=True):
            print(
                f"{c}: {b['status']} succeeded={b['counts']['succeeded']} "
                f"failed={b['counts']['failed']} cancelled={b['counts']['cancelled']}"
            )
        print(f"all {len(batches)} batches final in {elapsed:.1f}s")

        conn = await asyncpg.connect(args.database_url)
        try:
            over = await conn.fetch(OVERBOOKING_SQL, clients)
            dups = await conn.fetch(DUPLICATES_SQL, clients)
            confirmed = await conn.fetchval(
                "SELECT count(*) FROM reservations WHERE client_id = ANY($1::text[])", clients
            )
        finally:
            await conn.close()
        print(f"confirmed reservations: {confirmed}")
        print(f"overbooked property-nights (SQL check): {len(over)}")
        print(f"duplicate client_reference rows: {len(dups)}")
        expected = sum(b["counts"]["succeeded"] for b in batches)
        ok = not over and not dups and confirmed == expected and elapsed < args.deadline
        print("PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=20)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
