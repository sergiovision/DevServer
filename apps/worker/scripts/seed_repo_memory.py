#!/usr/bin/env python
"""Seed / prime a repo's Memory Knowledge Base (Pro).

The KB is normally built experientially as tasks run, but you can pre-load
durable facts and notes for a repo so its very first task starts informed —
and (re)build the cached wake-up digest on demand.

Run from apps/worker/:

    # Facts are "subject|predicate|object". Notes are free text.
    uv run python scripts/seed_repo_memory.py --repo-id 19 \
        --fact "tests|run with|cd FinCore && pytest -q" \
        --fact "entrypoint|is|FinCore/src/main.py" \
        --note "Local-only repo — never push; patch/untracked flows only." \
        --topic FinCore --build-digest

    # Just (re)build the wake-up digest from whatever the KB already holds:
    uv run python scripts/seed_repo_memory.py --repo-id 1 --build-digest

Requires the Pro package (services/pro/). Facts/notes are embedded with the
local model, so the first run may download it.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "src"))


async def main(args) -> None:
    from models.base import async_session
    try:
        from services.pro.repo_kb import RepoMemory
    except ImportError:
        print("Pro package not installed (services/pro/ absent) — nothing to seed.",
              file=sys.stderr)
        sys.exit(1)

    facts: list[tuple[str, str, str]] = []
    for raw in args.fact or []:
        parts = [p.strip() for p in raw.split("|")]
        if len(parts) != 3 or not parts[0] or not parts[1]:
            print(f"skip malformed --fact {raw!r} (need 'subject|predicate|object')",
                  file=sys.stderr)
            continue
        facts.append((parts[0], parts[1], parts[2]))

    async with async_session() as db:
        kb = RepoMemory(db, args.repo_id)

        for subj, pred, obj in facts:
            fid = await kb.record_fact(subj, pred, obj, topic=args.topic)
            print(f"  fact #{fid}: {subj} {pred} {obj}")

        for note in args.note or []:
            mid = await kb.remember(note, kind="note", topic=args.topic)
            print(f"  note #{mid}: {note[:80]}")

        if args.build_digest:
            digest = await kb.wake_up_digest(force=True)
            if digest:
                print("\n--- wake-up digest (repo %d) ---\n%s" % (args.repo_id, digest))
            else:
                print(f"\n(wake-up digest empty — repo {args.repo_id} has no KB content yet)")

    print(f"\nDone: {len(facts)} fact(s), {len(args.note or [])} note(s) for repo {args.repo_id}.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo-id", type=int, required=True, help="target repo id")
    ap.add_argument("--fact", action="append", metavar="S|P|O",
                    help="a fact 'subject|predicate|object' (repeatable)")
    ap.add_argument("--note", action="append", metavar="TEXT",
                    help="a durable note (repeatable)")
    ap.add_argument("--topic", default=None, help="topic/'room' tag for the seeded rows")
    ap.add_argument("--build-digest", action="store_true",
                    help="(re)build + cache the wake-up digest after seeding")
    asyncio.run(main(ap.parse_args()))
