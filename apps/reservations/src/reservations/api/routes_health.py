from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from reservations.api.deps import Pool
from reservations.api.problems import problem, request_id

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"status": "ok", "request_id": request_id()}


@router.get("/readyz", response_model=None)
async def readyz(pool: Pool) -> dict[str, Any] | JSONResponse:
    try:
        async with asyncio.timeout(2):
            await pool.fetchval("SELECT 1")
    except Exception as exc:
        return problem(503, f"database unreachable: {exc.__class__.__name__}")
    return {"status": "ready", "request_id": request_id()}
