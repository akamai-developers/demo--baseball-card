"""POST /cutout: a photo goes in, the person comes out on a transparent background.

Two backends behind one route.

  mock   card.procedural.plain_wall_cutout, a colour-distance matte. No weights,
         no GPU. It only works on the input this demo asks for, a subject
         against a plain wall.
  real   BiRefNet, fp16 on CUDA and fp32 on CPU. Any photo, any background.

The service holds a lock across the inference, because on the node it shares a
card with vLLM, and with FLUX too on a one-GPU node. Two models working at once
on one card is how 20 GB runs out.

The revision is pinned. BiRefNet is loaded with trust_remote_code=True, which
downloads birefnet.py from the Hub and executes it, so "whatever is on main
today" is not an acceptable answer to what this service runs.
"""

from __future__ import annotations

import asyncio
import io
import time

import numpy as np
from fastapi import Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from PIL import Image, ImageDraw, UnidentifiedImageError

from card.matte import refine_matte
from card.procedural import plain_wall_cutout
from common import __version__
from common.auth import require_api_key
from common.meta import model_headers
from common.service import ModelService, make_app
from common.settings import get_settings

MOCK_MODEL = "mock/plain-wall-matte"
MOCK_LICENSE = "n/a"

REAL_MODEL = "ZhengPeng7/BiRefNet"
REAL_LICENSE = "MIT"
REAL_REVISION = "e2bf8e4460fc8fa32bba5ea4d94b3233d367b0e4"

# What BiRefNet was trained on: a 1024x1024 square, ImageNet normalisation. The
# square is not a mistake and the aspect ratio is not preserved. The reference
# implementation resizes to (1024, 1024) with no letterboxing, so a 3:2 photo is
# squashed going in and the mask is stretched back on the way out.
#
# Letterboxing the photo instead was measured on the fixture portrait: 27.7 vs
# 27.8 percent of the frame called subject, and the same errors in the same
# places. It is no better, so this follows BiRefNet's own code.
INPUT_PX = 1024
IMAGENET_MEAN = (0.485, 0.456, 0.406)
IMAGENET_STD = (0.229, 0.224, 0.225)

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


class RealCutout(ModelService):
    """BiRefNet. 444 MB of weights, and 2,012 MiB of a card with one 1024 px
    pass, measured on a g2-gpu-rtx4000a2-s node on 2026-09-23."""

    def __init__(self, config) -> None:
        super().__init__(config, model=REAL_MODEL, licence=REAL_LICENSE)
        self._model = None
        self._dtype = None

    async def load(self) -> None:
        # Weights off disk and one warm-up. Both block, and the event loop has a
        # health check to answer, so they go to a thread.
        await asyncio.to_thread(self._load_blocking)
        self.loaded = True

    def _load_blocking(self) -> None:
        import torch
        from transformers import AutoModelForImageSegmentation

        # fp16 on the card, fp32 on a laptop. Half precision on a CPU is slower
        # than full, and several of the ops in here have no half CPU kernel at
        # all, so this is correctness and not only speed.
        self._dtype = torch.float16 if self.settings.device == "cuda" else torch.float32

        model = AutoModelForImageSegmentation.from_pretrained(
            REAL_MODEL, revision=REAL_REVISION, trust_remote_code=True
        )
        model.eval()
        model.to(device=self.settings.device, dtype=self._dtype)
        self._model = model

        # A real warm-up on a real frame. A solid rectangle segments to nothing
        # and skips most of the decoder, so the first request would still be the
        # one that pays for the lazily built kernels.
        self.cut(_warm_up_frame())

    def cut(self, image: Image.Image) -> Image.Image:
        import torch

        with torch.inference_mode():
            # forward() returns the decoder's predictions coarse to fine. The
            # last one is full resolution, and it is logits, so it needs a sigmoid.
            prediction = self._model(self._preprocess(image))[-1].sigmoid().float().cpu()

        raw = Image.fromarray(
            (prediction[0, 0].numpy() * 255.0).round().clip(0, 255).astype(np.uint8), "L"
        )
        # Back to the photo's own shape before the mask is cleaned, so an island
        # is measured against the subject as the card will see it, and the
        # feather is in the pixels that get saved.
        alpha = refine_matte(raw.resize(image.size, Image.BILINEAR))

        out = image.convert("RGBA")
        out.putalpha(alpha)
        return out

    def _preprocess(self, image: Image.Image):
        import torch

        square = image.convert("RGB").resize((INPUT_PX, INPUT_PX), Image.BILINEAR)
        pixels = np.asarray(square, dtype=np.float32) / 255.0
        pixels = (pixels - np.float32(IMAGENET_MEAN)) / np.float32(IMAGENET_STD)
        batch = torch.from_numpy(pixels.transpose(2, 0, 1)).unsqueeze(0)
        return batch.to(device=self.settings.device, dtype=self._dtype)


def _warm_up_frame(size: tuple[int, int] = (384, 512)) -> Image.Image:
    """Something with a subject in it, so the warm-up runs the whole decoder."""
    frame = Image.new("RGB", size, (206, 203, 198))
    draw = ImageDraw.Draw(frame)
    draw.ellipse([130, 60, 250, 210], fill=(122, 92, 74))
    draw.ellipse([70, 220, 310, 620], fill=(38, 44, 66))
    return frame


service: ModelService = RealCutout(settings) if settings.is_real else MockCutout(settings)

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
