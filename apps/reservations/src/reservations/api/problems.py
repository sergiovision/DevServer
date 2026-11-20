"""RFC 9457 problem+json responses and exception handlers."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from reservations.logs import current_context

logger = logging.getLogger(__name__)

PROBLEM_MEDIA_TYPE = "application/problem+json"

_TITLES = {
    400: "Bad Request",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    413: "Content Too Large",
    422: "Unprocessable Content",
    429: "Too Many Requests",
    500: "Internal Server Error",
    503: "Service Unavailable",
}


def request_id() -> str:
    return current_context().get("request_id", "")


def problem(
    status: int,
    detail: str,
    *,
    type_: str = "about:blank",
    title: str | None = None,
    headers: dict[str, str] | None = None,
    **extra: Any,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": type_,
        "title": title or _TITLES.get(status, "Error"),
        "status": status,
        "detail": detail,
        "request_id": request_id(),
    }
    body.update(extra)
    return JSONResponse(body, status_code=status, headers=headers, media_type=PROBLEM_MEDIA_TYPE)


class ProblemError(Exception):
    def __init__(
        self,
        status: int,
        detail: str,
        *,
        type_: str = "about:blank",
        headers: dict[str, str] | None = None,
        **extra: Any,
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.type_ = type_
        self.headers = headers
        self.extra = extra

    def response(self) -> JSONResponse:
        return problem(
            self.status, self.detail, type_=self.type_, headers=self.headers, **self.extra
        )


class BodyTooLargeError(StarletteHTTPException):
    def __init__(self, limit: int) -> None:
        super().__init__(413, f"request body exceeds {limit} bytes")


def loc_to_path(loc: tuple[Any, ...] | list[Any]) -> str:
    """('body', 'items', 3, 'check_out') -> 'items[3].check_out'."""
    parts = list(loc)
    if parts and parts[0] in ("body", "query", "path", "header"):
        parts = parts[1:]
    out = ""
    for p in parts:
        if isinstance(p, int):
            out += f"[{p}]"
        else:
            out += f".{p}" if out else str(p)
    return out or "$"


def validation_errors(errors: list[Any]) -> list[dict[str, Any]]:
    return [
        {"path": loc_to_path(e.get("loc", ())), "message": e.get("msg", ""), "type": e.get("type")}
        for e in errors
    ]


def install_handlers(app: FastAPI) -> None:
    @app.exception_handler(ProblemError)
    async def _problem(_: Request, exc: ProblemError) -> JSONResponse:
        return exc.response()

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return problem(exc.status_code, str(exc.detail), headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return problem(
            422,
            "request validation failed",
            type_="urn:problem:validation",
            errors=validation_errors(list(exc.errors())),
        )
