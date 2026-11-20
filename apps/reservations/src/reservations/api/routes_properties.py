from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Response

from reservations.api.deps import ClientId, Pool
from reservations.api.problems import ProblemError, request_id
from reservations.schemas import PropertyCreate, PropertyOut

router = APIRouter(tags=["properties"])


@router.post("/properties", status_code=201, response_model=PropertyOut)
async def create_property(
    body: PropertyCreate, client_id: ClientId, pool: Pool, response: Response
) -> PropertyOut:
    row = await pool.fetchrow(
        "INSERT INTO properties (client_id, name, capacity) VALUES ($1, $2, $3) "
        "RETURNING id, name, capacity, created_at",
        client_id,
        body.name,
        body.capacity,
    )
    response.headers["Location"] = f"/v1/properties/{row['id']}"
    return PropertyOut(request_id=request_id(), **dict(row))


@router.get("/properties/{property_id}", response_model=PropertyOut)
async def get_property(property_id: UUID, client_id: ClientId, pool: Pool) -> PropertyOut:
    row = await pool.fetchrow(
        "SELECT id, name, capacity, created_at FROM properties WHERE id = $1 AND client_id = $2",
        property_id,
        client_id,
    )
    if row is None:
        raise ProblemError(404, "property not found")
    return PropertyOut(request_id=request_id(), **dict(row))
