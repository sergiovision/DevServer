#!/usr/bin/env python
"""Backfill agent_memory embeddings with the local model.

Run after switching from the Voyage cloud API to local fastembed
embeddings (services/embeddings.py). The dimension migration in
001_initial.sql drops the old 1536-dim vectors, so every row needs a fresh
768-dim embedding. This script regenerates embeddings for any row whose
``embedding`` is NULL.

Idempotent and resumable — re-running only touches rows still missing a
vector. Safe to run while the worker is live.

Usage (from apps/worker/):
    uv run python scripts/reembed_memory.py
    uv run python scripts/reembed_memory.py --all   # re-embed every row
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

# Make the worker `src/` package importable (config, services.*).
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "src"))

import asyncpg  # noqa: E402

from config import settings  # noqa: E402
from services import embeddings  # noqa: E402

_BATCH = 64


async def main(reembed_all: bool) -> None:
    dsn = settings.database_url
    if not dsn:
        print("DATABASE_URL not configured", file=sys.stderr)
        sys.exit(1)

    print(f"Embedding model: {embeddings.MODEL_ID} ({embeddings.DIM}d)")
    await embeddings.warm_up()

    conn = await asyncpg.connect(dsn)
    try:
        where = "" if reembed_all else "WHERE embedding IS NULL"
        rows = await conn.fetch(
            f"SELECT id, content FROM agent_memory {where} ORDER BY id"
        )
        total = len(rows)
        if not total:
            print("Nothing to re-embed.")
            return
        print(f"Re-embedding {total} row(s)...")

        done = 0
        for start in range(0, total, _BATCH):
            chunk = rows[start : start + _BATCH]
            vecs = await embeddings.embed_batch([r["content"] or "" for r in chunk])
            for row, vec in zip(chunk, vecs):
                if vec is None:
                    continue
                await conn.execute(
                    "UPDATE agent_memory SET embedding = $1::vector WHERE id = $2",
                    str(list(vec)),
                    row["id"],
                )
                done += 1
            print(f"  {min(start + _BATCH, total)}/{total} processed", flush=True)

        print(f"Done: {done}/{total} row(s) re-embedded.")
    finally:
        await conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--all",
        action="store_true",
        help="re-embed every row, not just rows missing a vector",
    )
    args = ap.parse_args()
    asyncio.run(main(args.all))
