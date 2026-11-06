"""Settings read from the environment, and from .env when one is next to the process.

Every service reads the same class and uses the fields it needs. In the stack,
compose hands each container the repo's .env plus a few lines of its own. On a
laptop, cardctl and the tests read the .env at the repo root.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Backend = Literal["mock", "real"]
Device = Literal["cuda", "cpu"]


class Settings(BaseSettings):
    """Configuration for one running service."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Auth. One key for every service, vLLM included.
    card_api_key: str = ""

    # What this service is.
    service_name: str = "api"
    backend: Backend = "mock"
    device: Device = "cpu"
    # Copied into the X-Ran-On header and into the ledger on the back of the
    # card: akamai-gpu0, akamai-gpu1 or akamai-cpu on the node, local-cpu for
    # the real BiRefNet on a laptop, or a mock- label.
    gpu_label: str = "mock"

    # Where a service keeps its files. The api: the database, the uploads and
    # the finished cards. The art service: its pool of painted backgrounds.
    data_dir: Path = Path("/data")
    # The sweeper deletes a card, its photo with it, this many hours after it
    # was made, and looks for such cards every sweep_every_s seconds.
    retention_hours: int = Field(default=24, ge=1)
    sweep_every_s: int = Field(default=900, ge=10)

    # Internal service addresses. Empty means "not part of this stack": the api
    # skips the health check and the pipeline step for an empty URL.
    cutout_url: str = ""
    art_url: str = ""
    llm_url: str = ""
    # vLLM is a stock image. It serves a model and says nothing about its
    # licence or which card it runs on, and the back of the card needs both, so
    # the stack is told here. llm_model is also the name every request asks
    # for: vLLM answers 404 to a name it does not serve.
    llm_model: str = ""
    llm_license: str = ""
    llm_ran_on: str = ""

    # Per-step ceilings in seconds.
    step_timeout_s: int = Field(default=180, ge=1)
    art_timeout_s: int = Field(default=120, ge=1)

    # FLUX.2 klein takes 17,956 MiB of a card on its own. The one-GPU layout
    # turns this on: the weights stay in host memory and each layer is copied
    # to the card as it runs, which makes every painting slower.
    art_cpu_offload: bool = False

    # How many finished backgrounds per style the art service keeps on disk,
    # painted while nothing else wants the GPU. A card takes one and the service
    # paints a replacement. 0 turns the pool off, and every card waits for its
    # own painting.
    art_pool_size: int = Field(default=3, ge=0, le=12)

    # How many players "Swings like" names. 0 turns the section off.
    comps_count: int = Field(default=3, ge=0, le=10)

    # How many cards may be in Chromium at the same time. Each is two pages in
    # the api's one Chromium.
    render_concurrency: int = Field(default=2, ge=1, le=8)

    # The browser app the api serves, and the three model repos with their
    # pinned commits, which GET /models reads. Relative paths resolve against
    # the working directory: /app in the container, the repo root on a laptop.
    web_dir: Path = Path("web")
    models_env: Path = Path("deploy/models.env")

    log_level: str = "INFO"

    # Uploads. Caddy caps a request body at 20 MB before the api sees it.
    max_upload_mb: int = Field(default=15, ge=1, le=19)
    max_photo_px: int = Field(default=2048, ge=512, le=4096)

    @field_validator("card_api_key")
    @classmethod
    def _strip_key(cls, value: str) -> str:
        return value.strip()

    @property
    def service_urls(self) -> dict[str, str]:
        """The model services this api talks to. Only the configured ones."""
        pairs = {"cutout": self.cutout_url, "art": self.art_url, "llm": self.llm_url}
        return {name: url.rstrip("/") for name, url in pairs.items() if url}

    @property
    def is_real(self) -> bool:
        return self.backend == "real"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """One Settings instance per process. Call get_settings.cache_clear() in tests."""
    return Settings()
