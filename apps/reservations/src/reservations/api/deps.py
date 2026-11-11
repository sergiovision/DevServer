"""Shared FastAPI dependencies."""

from __future__ import annotations

from typing import Annotated

import asyncpg
from fastapi import Depends, Request

from reservations.config import Settings


def get_pool(request: Request) -> asyncpg.Pool:
    return request.app.state.pool


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_client_id(request: Request) -> str:
    # Presence and format are enforced by RequestContextMiddleware for /v1.
    return request.headers["x-client-id"]


Pool = Annotated[asyncpg.Pool, Depends(get_pool)]
AppSettings = Annotated[Settings, Depends(get_app_settings)]
ClientId = Annotated[str, Depends(get_client_id)]
