"""Pydantic request/response models and batch-level semantic validation."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

CLIENT_ID_RE = re.compile(r"^[a-z0-9-]{3,64}$")

BatchStatus = Literal["accepted", "processing", "completed", "completed_with_errors", "cancelled"]
ItemStatus = Literal["pending", "processing", "succeeded", "failed", "cancelled"]
FINAL_BATCH_STATUSES: tuple[str, ...] = ("completed", "completed_with_errors", "cancelled")


# --------------------------------------------------------------------- requests


class PropertyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    capacity: int = Field(gt=0, le=100_000)


class BatchItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_reference: str = Field(min_length=1, max_length=128)
    property_id: UUID
    guest_name: str = Field(min_length=1, max_length=200)
    guests: int = Field(gt=0, le=1000)
    check_in: date
    check_out: date
    group_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def _dates(self) -> BatchItemIn:
        if self.check_out <= self.check_in:
            raise ValueError("check_out must be after check_in")
        return self


class BatchCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    callback_url: HttpUrl | None = None
    items: list[BatchItemIn]


def semantic_errors(batch: BatchCreate, *, chunk_size: int) -> list[dict[str, Any]]:
    """Cross-item checks pydantic cannot express per field. Paths match pydantic's."""
    errors: list[dict[str, Any]] = []
    first_seen: dict[str, int] = {}
    group_sizes: dict[str, list[int]] = {}
    for idx, item in enumerate(batch.items):
        prev = first_seen.setdefault(item.client_reference, idx)
        if prev != idx:
            errors.append(
                {
                    "path": f"items[{idx}].client_reference",
                    "message": f"duplicate client_reference (also at items[{prev}])",
                    "type": "duplicate_client_reference",
                }
            )
        if item.group_id is not None:
            group_sizes.setdefault(item.group_id, []).append(idx)
    for group_id, members in group_sizes.items():
        # A group is booked atomically inside one chunk, so it must fit in one.
        if len(members) > chunk_size:
            errors.append(
                {
                    "path": f"items[{members[0]}].group_id",
                    "message": f"group {group_id!r} has {len(members)} items; max is {chunk_size}",
                    "type": "group_too_large",
                }
            )
    return errors


# -------------------------------------------------------------------- responses


class PropertyOut(BaseModel):
    request_id: str
    id: UUID
    name: str
    capacity: int
    created_at: datetime


class BatchLinks(BaseModel):
    self: str
    items: str


class BatchCounts(BaseModel):
    succeeded: int
    failed: int
    cancelled: int
    pending: int
    processing: int


class BatchOut(BaseModel):
    request_id: str
    batch_id: UUID
    status: BatchStatus
    total_items: int
    counts: BatchCounts
    callback_url: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    links: BatchLinks


class BatchItemOut(BaseModel):
    seq: int
    client_reference: str
    group_id: str | None
    status: ItemStatus
    reservation_id: UUID | None
    error_code: str | None
    error_message: str | None
    error_details: dict[str, Any] | None
    attempts: int


class BatchItemsPage(BaseModel):
    request_id: str
    batch_id: UUID
    items: list[BatchItemOut]
    next_cursor: str | None


class ReservationOut(BaseModel):
    request_id: str
    id: UUID
    property_id: UUID
    client_reference: str
    guest_name: str
    guests: int
    check_in: date
    check_out: date
    status: str
    batch_item_id: UUID | None
    created_at: datetime
