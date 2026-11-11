"""FastAPI app: ``uvicorn reservations.api.app:app``."""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from reservations.api import routes_batches, routes_health, routes_properties
from reservations.api.problems import BodyTooLargeError, install_handlers, problem
from reservations.config import Settings, get_settings
from reservations.db import close_pool, create_pool
from reservations.logs import reset_context, set_context, setup_logging
from reservations.schemas import CLIENT_ID_RE

logger = logging.getLogger(__name__)

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class RequestContextMiddleware:
    """Pure ASGI middleware: request id, X-Client-Id check, streamed body-size cap.

    The body cap counts bytes as they are received, so a missing or lying
    Content-Length does not get around it.
    """

    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        incoming_id = headers.get("x-request-id", "")
        request_id = incoming_id if _REQUEST_ID_RE.fullmatch(incoming_id) else uuid.uuid4().hex
        client_id = headers.get("x-client-id")
        is_api = scope["path"].startswith("/v1/") or scope["path"] == "/v1"
        token = set_context(request_id=request_id, client_id=client_id if is_api else None)
        response_started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                MutableHeaders(scope=message).append("X-Request-Id", request_id)
            await send(message)

        try:
            if is_api and (client_id is None or not CLIENT_ID_RE.fullmatch(client_id)):
                resp = problem(
                    400, "X-Client-Id header is required and must match ^[a-z0-9-]{3,64}$"
                )
                await resp(scope, receive, send_wrapper)
                return

            length = headers.get("content-length")
            if length is not None and length.isdigit() and int(length) > self.max_body_bytes:
                await problem(413, f"request body exceeds {self.max_body_bytes} bytes")(
                    scope, receive, send_wrapper
                )
                return

            received = 0

            async def receive_wrapper() -> Message:
                nonlocal received
                message = await receive()
                if message["type"] == "http.request":
                    received += len(message.get("body", b""))
                    if received > self.max_body_bytes:
                        raise BodyTooLargeError(self.max_body_bytes)
                return message

            await self.app(scope, receive_wrapper, send_wrapper)
        except BodyTooLargeError as exc:
            if response_started:
                raise
            await problem(413, str(exc.detail))(scope, receive, send_wrapper)
        except Exception:
            # Handled here rather than in ServerErrorMiddleware so the 500 still
            # carries the request id.
            logger.exception("unhandled error")
            if response_started:
                raise
            await problem(500, "internal error")(scope, receive, send_wrapper)
        finally:
            reset_context(token)


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        setup_logging(cfg.log_level)
        app.state.pool = await create_pool(cfg)
        try:
            yield
        finally:
            await close_pool(app.state.pool)

    app = FastAPI(title="Bulk Reservations API", version="0.1.0", lifespan=lifespan)
    app.state.settings = cfg
    install_handlers(app)
    app.include_router(routes_health.router)
    app.include_router(routes_properties.router, prefix="/v1")
    app.include_router(routes_batches.router, prefix="/v1")
    app.add_middleware(RequestContextMiddleware, max_body_bytes=cfg.max_body_bytes)
    return app


app = create_app()
