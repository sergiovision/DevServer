#!/usr/bin/env python
"""Seed a repo's memory knowledge base before its first task.

The knowledge base normally grows as tasks run, but you can pre-load durable
facts and notes so the very first task starts informed — and (re)build the
cached wake-up digest on demand.

Usage (from apps/worker/):

    # Facts are "subject|predicate|object". Notes are free text.
    uv run python scripts/seed_repo_memory.py --repo-id 19 \\
        --fact "tests|run with|pytest -q" \\
        --fact "entrypoint|is|src/main.py" \\
        --note "Local-only repo — never push; patch/untracked flows only." \\
        --topic src --build-digest

    # Just (re)build the wake-up digest from what the knowledge base holds:
    uv run python scripts/seed_repo_memory.py --repo-id 1 --build-digest

Facts and notes are embedded with the local model, so the first run may
download it.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "src"))

from models.base import async_session  # noqa: E402
from services.repo_kb import RepoMemory  # noqa: E402


def parse_fact(raw: str) -> tuple[str, str, str] | None:
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) != 3 or not parts[0] or not parts[1]:
        return None
    return parts[0], parts[1], parts[2]


async def main(args: argparse.Namespace) -> None:
    facts: list[tuple[str, str, str]] = []
    for raw in args.fact or []:
        fact = parse_fact(raw)
        if fact is None:
            print(
                f"skip malformed --fact {raw!r} (need 'subject|predicate|object')", file=sys.stderr
            )
            continue
        facts.append(fact)

    async with async_session() as db:
        kb = RepoMemory(db, args.repo_id)
        for subject, predicate, obj in facts:
            fact_id = await kb.record_fact(
                subject, predicate, obj, topic=args.topic, metadata={"source": "seed"}
            )
            print(f"  fact #{fact_id}: {subject} {predicate} {obj}")
        for note in args.note or []:
            mem_id = await kb.remember(
                note, kind="note", topic=args.topic, metadata={"source": "seed"}
            )
            print(f"  note #{mem_id}: {note[:80]}")
        if args.build_digest:
            digest = await kb.wake_up_digest(force=True)
            if digest:
                print(f"\n--- wake-up digest (repo {args.repo_id}) ---\n{digest}")
            else:
                print(f"\n(wake-up digest empty — repo {args.repo_id} has no knowledge yet)")

    print(f"\nDone: {len(facts)} fact(s), {len(args.note or [])} note(s) for repo {args.repo_id}.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--repo-id", type=int, required=True, help="target repo id")
    ap.add_argument(
        "--fact", action="append", metavar="S|P|O", help="'subject|predicate|object' (repeatable)"
    )
    ap.add_argument("--note", action="append", metavar="TEXT", help="a durable note (repeatable)")
    ap.add_argument("--topic", default=None, help="topic tag for the seeded rows")
    ap.add_argument(
        "--build-digest", action="store_true", help="(re)build the cached wake-up digest"
    )
    asyncio.run(main(ap.parse_args()))
