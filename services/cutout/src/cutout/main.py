"""POST /cutout: a photo goes in, the person comes out on a transparent background.

  mock   card.procedural.plain_wall_cutout, a colour-distance matte. No weights,
         no GPU. It only works on the input this demo asks for, a subject
         against a plain wall.

The service holds a lock across the inference, because on the node it shares a
card with the other models, and two of them working at once is how 20 GB runs
out.
"""

from __future__ import annotations

import asyncio
import io
import time

from fastapi import Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from PIL import Image, UnidentifiedImageError

from card.procedural import plain_wall_cutout
from common import __version__
from common.auth import require_api_key
from common.meta import model_headers
from common.service import ModelService, make_app
from common.settings import get_settings

MOCK_MODEL = "mock/plain-wall-matte"
MOCK_LICENSE = "n/a"

settings = get_settings()


class MockCutout(ModelService):
    """The matte. Deterministic, and about 200 ms on a 2048 px photo."""

    def __init__(self, config) -> None:
        super().__init__(config, model=MOCK_MODEL, licence=MOCK_LICENSE)

    async def load(self) -> None:
        # One warm-up, so the first real request is not the one that imports numpy.
        await asyncio.to_thread(plain_wall_cutout, Image.new("RGB", (64, 64), (200, 200, 200)))
        self.loaded = True

    def cut(self, image: Image.Image) -> Image.Image:
        return plain_wall_cutout(image)


service = MockCutout(settings)

app = make_app("baseball-card cutout", __version__, settings, on_start=service.startup)


@app.get("/healthz", dependencies=[Depends(require_api_key)])
async def healthz() -> dict:
    """Model, backend, device, GPU, loaded, vram_mb. The api copies these."""
    return service.health()


@app.post("/cutout", dependencies=[Depends(require_api_key)])
async def cut(image: UploadFile = File(..., description="the photo")) -> Response:
    """An RGBA PNG of the subject, with the background gone."""
    if not service.loaded:
        why = service.load_error or "still loading"
        raise HTTPException(503, f"the model is not loaded: {why}")

    raw = await image.read()
    try:
        with Image.open(io.BytesIO(raw)) as opened:
            opened.load()
            source = opened.convert("RGB")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise HTTPException(400, f"that is not an image this service can read: {exc}") from exc

    started = time.perf_counter()
    async with service.lock:
        result = await asyncio.to_thread(service.cut, source)
    elapsed_ms = (time.perf_counter() - started) * 1000

    buffer = io.BytesIO()
    result.save(buffer, "PNG")
    return Response(
        buffer.getvalue(),
        media_type="image/png",
        headers=model_headers(service.model, service.licence, elapsed_ms, service.ran_on),
    )
