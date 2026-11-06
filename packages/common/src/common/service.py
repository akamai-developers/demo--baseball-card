"""The shape every model service shares.

`cutout` and `art` are the same program twice: take bytes in, run one model,
put bytes out, and report honestly on /healthz. This holds the parts that do
not differ, so each service file is only the model. `gpu-stats` and the mock
of vLLM use make_app() alone.

Two rules the GPU node depends on:

  one at a time    Each service holds an asyncio.Lock across its inference. Two
                   models share a card, and two CUDA contexts plus a second
                   batch is how you run out of 20 GB.
  loaded means     A service reports loaded: true only after the weights are in
  loaded           memory and one warm-up inference has finished. `make deploy`
                   waits on that, so it must not be optimistic.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from common.auth import add_auth_gate, assert_key_is_usable
from common.httplog import add_access_log, setup_logging
from common.settings import Settings

log = logging.getLogger(__name__)


def describe_device(device: str) -> tuple[str | None, int | None]:
    """(gpu name, VRAM in use) as torch sees it. (None, None) without CUDA.

    torch is imported here and not at module scope, because the mock images do
    not have it and never need it.
    """
    if device != "cuda":
        return None, None
    try:
        import torch
    except ImportError:
        return None, None
    if not torch.cuda.is_available():
        return None, None
    used_bytes = torch.cuda.memory_reserved()
    return torch.cuda.get_device_name(0), int(used_bytes // (1024 * 1024))


class ModelService:
    """One model, loaded once, used by one request at a time."""

    def __init__(self, settings: Settings, *, model: str, licence: str) -> None:
        self.settings = settings
        self.model = model
        self.licence = licence
        self.lock = asyncio.Lock()
        self.loaded = False
        self.load_error: str | None = None
        self.gpu: str | None = None
        self.vram_mb: int | None = None

    @property
    def ran_on(self) -> str:
        """What goes in X-Ran-On and onto the back of the card."""
        return self.settings.gpu_label

    async def load(self) -> None:
        """Put the weights in memory and run one warm-up. Override in the service."""
        self.loaded = True

    async def startup(self) -> None:
        """Load, then record what the device turned out to be. Never raises."""
        started = time.perf_counter()
        try:
            await self.load()
        except Exception as exc:  # the service stays up and says why on /healthz
            self.load_error = f"{type(exc).__name__}: {exc}"
            log.exception("%s failed to load %s", self.settings.service_name, self.model)
            return
        self.gpu, self.vram_mb = describe_device(self.settings.device)
        log.info(
            "%s loaded %s in %.1fs on %s",
            self.settings.service_name,
            self.model,
            time.perf_counter() - started,
            self.gpu or self.settings.device,
        )

    def health(self) -> dict:
        """The body of /healthz. The api copies these fields into its own report."""
        return {
            "status": "ok" if self.loaded else ("error" if self.load_error else "loading"),
            "model": self.model,
            "backend": self.settings.backend,
            "device": self.settings.device,
            "gpu": self.gpu,
            "ran_on": self.ran_on,
            "loaded": self.loaded,
            "vram_mb": self.vram_mb,
            "error": self.load_error,
        }


def make_app(
    title: str,
    version: str,
    settings: Settings,
    *,
    open_paths: frozenset[str] = frozenset({"/livez"}),
    on_start: Callable[[], object] | None = None,
) -> FastAPI:
    """A FastAPI app with the key gate, the access log, and /livez. No docs page.

    `on_start` is awaited during startup if it returns an awaitable, so a
    service can load its model there.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        setup_logging(settings.log_level)
        assert_key_is_usable(settings.card_api_key, settings.backend, settings.service_name)
        if on_start is not None:
            result = on_start()
            if asyncio.iscoroutine(result):
                await result
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


__all__ = ["ModelService", "describe_device", "make_app"]
