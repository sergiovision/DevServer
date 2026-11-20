"""Availability check + reservation insert. Plain SQL, no queue knowledge.

Callers must run :func:`lock_properties` and :func:`book` inside one
transaction. The property row lock (``FOR UPDATE``) serializes every booking of
a property, so the overlap count and the insert cannot race (no overbooking).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from uuid import UUID

PROPERTY_NOT_FOUND = "PROPERTY_NOT_FOUND"
NO_AVAILABILITY = "NO_AVAILABILITY"
DUPLICATE_CLIENT_REFERENCE = "DUPLICATE_CLIENT_REFERENCE"
GROUP_FAILED = "GROUP_FAILED"
INTERNAL_RETRY_EXHAUSTED = "INTERNAL_RETRY_EXHAUSTED"
INTERNAL_ERROR = "INTERNAL_ERROR"


class BookingError(Exception):
    """A business failure: final, never retried."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


@dataclass(frozen=True, slots=True)
class BookingRequest:
    item_id: UUID
    client_id: str
    client_reference: str
    property_id: UUID
    guest_name: str
    guests: int
    check_in: date
    check_out: date
    group_id: str | None = field(default=None)

    @classmethod
    def from_item(
        cls, item_id: UUID, client_id: str, payload: str | dict[str, Any]
    ) -> BookingRequest:
        data = json.loads(payload) if isinstance(payload, str) else payload
        return cls(
            item_id=item_id,
            client_id=client_id,
            client_reference=data["client_reference"],
            property_id=UUID(data["property_id"]),
            guest_name=data["guest_name"],
            guests=int(data["guests"]),
            check_in=date.fromisoformat(data["check_in"]),
            check_out=date.fromisoformat(data["check_out"]),
            group_id=data.get("group_id"),
        )


async def lock_properties(
    conn: Any, client_id: str, property_ids: Iterable[UUID]
) -> dict[UUID, int]:
    """Lock the client's properties in ascending id order; return id -> capacity.

    Ids that do not exist or belong to another client are simply absent.
    Ascending order is what prevents deadlocks between multi-property groups.
    """
    ids = sorted(set(property_ids))
    if not ids:
        return {}
    rows = await conn.fetch(
        "SELECT id, capacity FROM properties "
        "WHERE id = ANY($1::uuid[]) AND client_id = $2 ORDER BY id FOR UPDATE",
        ids,
        client_id,
    )
    return {r["id"]: r["capacity"] for r in rows}


async def book(conn: Any, req: BookingRequest, capacities: dict[UUID, int]) -> UUID:
    """Book one reservation. Raises :class:`BookingError` on a business failure.

    Has no side effects when it raises, so callers may keep using the
    transaction afterwards.
    """
    capacity = capacities.get(req.property_id)
    if capacity is None:
        raise BookingError(PROPERTY_NOT_FOUND, f"property {req.property_id} not found")

    overlapping: int = await conn.fetchval(
        """
        SELECT count(*) FROM reservations
         WHERE property_id = $1 AND status = 'confirmed'
           AND check_in < $3 AND check_out > $2
        """,
        req.property_id,
        req.check_in,
        req.check_out,
    )
    if overlapping >= capacity:
        raise BookingError(
            NO_AVAILABILITY,
            f"no availability for {req.check_in}..{req.check_out} ({overlapping}/{capacity} booked)",
        )

    reservation_id: UUID | None = await conn.fetchval(
        """
        INSERT INTO reservations (client_id, property_id, client_reference, guest_name, guests,
                                  check_in, check_out, status, batch_item_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7, 'confirmed', $8)
        ON CONFLICT (client_id, client_reference) DO NOTHING
        RETURNING id
        """,
        req.client_id,
        req.property_id,
        req.client_reference,
        req.guest_name,
        req.guests,
        req.check_in,
        req.check_out,
        req.item_id,
    )
    if reservation_id is None:
        existing = await conn.fetchval(
            "SELECT id FROM reservations WHERE client_id = $1 AND client_reference = $2",
            req.client_id,
            req.client_reference,
        )
        raise BookingError(
            DUPLICATE_CLIENT_REFERENCE,
            f"client_reference {req.client_reference!r} already booked as reservation {existing}",
            {"reservation_id": str(existing)},
        )
    return reservation_id
