"""The shape every model service shares: the key gate, the access log, /livez.

The api builds its own app the same way, with more routes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from common.auth import add_auth_gate, assert_key_is_usable
from common.httplog import add_access_log, setup_logging
from common.settings import Settings


def make_app(
    title: str,
    version: str,
    settings: Settings,
    *,
    open_paths: frozenset[str] = frozenset({"/livez"}),
) -> FastAPI:
    """A FastAPI app with the key gate, the access log, and /livez. No docs page."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        setup_logging(settings.log_level)
        assert_key_is_usable(settings.card_api_key, settings.backend, settings.service_name)
        yield

    app = FastAPI(
        title=title,
        version=version,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    # The gate first, so the access log wraps it and records the 401s too.
    add_auth_gate(app, open_paths)
    add_access_log(app)

    @app.get("/livez", response_class=PlainTextResponse)
    async def livez() -> str:
        return "ok"

    return app


__all__ = ["make_app"]
