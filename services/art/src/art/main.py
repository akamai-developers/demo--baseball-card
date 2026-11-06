"""POST /art/generate: a prompt goes in, a background comes out.

  mock   card.procedural.stadium, drawn with numpy and Pillow from the style
         name and the seed. No weights, no GPU, about 200 ms.

The request carries both `prompt` and `style`. A model paints the prompt. The
mock cannot read English, so it uses the style name to pick a palette. The seed
decides everything else, so the same request always gets the same picture.
X-Art-Source says `fresh`: this one was painted for the request.

Nothing here writes a letter on the image. Every word on the card is laid out in
HTML by the api.
"""

from __future__ import annotations

import asyncio
import io
import time

from fastapi import Depends, HTTPException
from fastapi.responses import Response
from PIL import Image
from pydantic import BaseModel, Field

from card.procedural import stadium
from common import __version__
from common.auth import require_api_key
from common.meta import ART_SOURCE, model_headers
from common.schemas import STYLES
from common.service import ModelService, make_app
from common.settings import get_settings

MOCK_MODEL = "mock/procedural-stadium"
MOCK_LICENSE = "n/a"

settings = get_settings()


class ArtRequest(BaseModel):
    """What the api sends. Sizes are capped so one request cannot fill the card."""

    prompt: str = Field(min_length=1, max_length=2000)
    # Not part of the prompt. The mock uses it to choose a palette.
    style: str = Field(default=STYLES[0], max_length=40)
    seed: int = Field(default=0, ge=0, le=2**31 - 1)
    width: int = Field(default=1024, ge=256, le=1536)
    height: int = Field(default=1408, ge=256, le=1536)
    steps: int = Field(default=4, ge=1, le=50)


class MockArt(ModelService):
    """The drawn ballpark. Deterministic in the seed."""

    def __init__(self, config) -> None:
        super().__init__(config, model=MOCK_MODEL, licence=MOCK_LICENSE)

    async def load(self) -> None:
        # A real warm-up, not a token one. stadium() draws bands and a crowd
        # speckle that need a frame with some height to it.
        await asyncio.to_thread(stadium, STYLES[0], 256, 352, 0)
        self.loaded = True

    def paint(self, request: ArtRequest) -> Image.Image:
        return stadium(request.style, request.width, request.height, seed=request.seed)


service = MockArt(settings)

app = make_app("baseball-card art", __version__, settings, on_start=service.startup)


@app.get("/healthz", dependencies=[Depends(require_api_key)])
async def healthz() -> dict:
    """Model, backend, device, GPU, loaded, vram_mb. The api copies these."""
    return service.health()


def png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


@app.post("/art/generate", dependencies=[Depends(require_api_key)])
async def generate(request: ArtRequest) -> Response:
    """A PNG background at the requested size. No text, no people."""
    if not service.loaded:
        why = service.load_error or "still loading"
        raise HTTPException(503, f"the model is not loaded: {why}")

    started = time.perf_counter()
    async with service.lock:
        image = await asyncio.to_thread(service.paint, request)
    elapsed_ms = (time.perf_counter() - started) * 1000

    # In a thread: 2 MB of PNG takes half a second to write, and the event loop
    # has health checks to answer meanwhile.
    data = await asyncio.to_thread(png_bytes, image)
    headers = model_headers(service.model, service.licence, elapsed_ms, service.ran_on)
    headers[ART_SOURCE] = "fresh"
    return Response(data, media_type="image/png", headers=headers)
