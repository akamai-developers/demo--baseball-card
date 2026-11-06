"""The orchestrator API.

    GET  /livez     open. Says ok, and nothing else
    GET  /healthz   every service, as it describes itself
    GET  /models    the three models, read from deploy/models.env

Everything except /livez needs `Authorization: Bearer $CARD_API_KEY`. The gate
in common.auth runs before routing, so a path that does not exist answers 401
rather than telling a stranger which paths do.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import PlainTextResponse

from api import catalog
from api.health import probe_all
from common import __version__
from common.auth import add_auth_gate, assert_key_is_usable, require_api_key
from common.httplog import add_access_log, setup_logging
from common.schemas import HealthReport
from common.settings import Settings, get_settings

log = logging.getLogger("api")

# Served without the key. Every other path, one that does not exist included,
# answers 401 without a key.
OPEN_PATHS = frozenset({"/livez"})


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    setup_logging(settings.log_level)
    assert_key_is_usable(settings.card_api_key, settings.backend, "api")
    # Read once. A models.env that is missing or lacks a pin stops the api
    # here, with a sentence that names the file, and not at the first request.
    app.state.catalog = catalog.build(settings)
    log.info(
        "api %s up. backend=%s services=%s",
        __version__,
        settings.backend,
        ",".join(settings.service_urls) or "none",
    )
    yield


app = FastAPI(
    title="baseball-card api",
    version=__version__,
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# The gate first, so the access log wraps it and records the 401s too.
add_auth_gate(app, open_paths=OPEN_PATHS)
add_access_log(app)


@app.get("/livez", response_class=PlainTextResponse)
async def livez() -> str:
    """The one open route. It says ok and nothing else."""
    return "ok"


@app.get("/healthz", response_model=HealthReport, dependencies=[Depends(require_api_key)])
async def healthz(settings: Settings = Depends(get_settings)) -> HealthReport:
    """What every service reports about itself: model, backend, device, GPU, VRAM."""
    services = await probe_all(settings.service_urls, settings.card_api_key, settings.llm_ran_on)
    return HealthReport(
        ok=all(s.status == "ok" for s in services.values()),
        service="api",
        version=__version__,
        backend=settings.backend,
        services=services,
    )


@app.get("/models", response_model=catalog.Catalog, dependencies=[Depends(require_api_key)])
async def models(request: Request) -> catalog.Catalog:
    """The models a card runs through, with the commit the node loads."""
    return request.app.state.catalog
