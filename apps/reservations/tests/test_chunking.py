"""Unit tests for chunk planning (no database)."""

from __future__ import annotations

import uuid

from reservations.chunking import ChunkItem, plan_chunks


def _items(groups: list[str | None]) -> list[ChunkItem]:
    return [ChunkItem(uuid.uuid4(), seq, g) for seq, g in enumerate(groups)]


def test_plain_items_are_cut_at_chunk_size():
    chunks = plan_chunks(_items([None] * 60), 25)
    assert [len(c) for c in chunks] == [25, 25, 10]


def test_groups_are_never_split():
    items = _items([None] * 20 + ["g"] * 10 + [None] * 3)
    chunks = plan_chunks(items, 25)
    group_ids = {i.id for i in items if i.group_id == "g"}
    holders = [c for c in chunks if group_ids & set(c)]
    assert len(holders) == 1 and group_ids <= set(holders[0])
    assert all(len(c) <= 25 for c in chunks)
    assert sum(len(c) for c in chunks) == len(items)


def test_non_contiguous_group_members_stay_together():
    items = _items(["g", None, None, "g", None, "g"])
    chunks = plan_chunks(items, 3)
    group_ids = {i.id for i in items if i.group_id == "g"}
    assert any(group_ids == set(c) for c in chunks)
    assert sum(len(c) for c in chunks) == 6
