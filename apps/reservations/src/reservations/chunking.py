"""Split batch items into chunks without ever splitting a ``group_id`` group."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ChunkItem:
    id: UUID
    seq: int
    group_id: str | None


def plan_chunks(items: Sequence[ChunkItem], chunk_size: int) -> list[list[UUID]]:
    """Greedy packing of units (a lone item, or a whole group) in ``seq`` order.

    A group is placed where its first member appears. A unit that does not fit
    in the current chunk starts a new one; a unit is never larger than
    ``chunk_size`` because the API rejects such groups.
    """
    units: list[list[UUID]] = []
    group_unit: dict[str, list[UUID]] = {}
    for item in sorted(items, key=lambda i: i.seq):
        if item.group_id is None:
            units.append([item.id])
        elif item.group_id in group_unit:
            group_unit[item.group_id].append(item.id)
        else:
            unit = [item.id]
            group_unit[item.group_id] = unit
            units.append(unit)

    chunks: list[list[UUID]] = []
    current: list[UUID] = []
    for unit in units:
        if current and len(current) + len(unit) > chunk_size:
            chunks.append(current)
            current = []
        current.extend(unit)
    if current:
        chunks.append(current)
    return chunks
