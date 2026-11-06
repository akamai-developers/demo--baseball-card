"""Access logging that records method, path, status and milliseconds. Nothing else.

No headers, no bodies, no query strings. The API key travels in a header, so a
logger that prints headers would leak it into the container logs. A photo's
file name travels in a body, and stays there too.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response

log = logging.getLogger("access")


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def add_access_log(app: FastAPI) -> None:
    """Attach the access log middleware."""

    @app.middleware("http")
    async def _access_log(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            elapsed_ms = (time.perf_counter() - started) * 1000
            log.exception("%s %s 500 %.1fms", request.method, request.url.path, elapsed_ms)
            raise
        elapsed_ms = (time.perf_counter() - started) * 1000
        log.info(
            "%s %s %d %.1fms",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
        )
        return response
