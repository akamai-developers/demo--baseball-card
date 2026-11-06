"""POST /art/generate: a prompt goes in, a background comes out.

Two backends behind one route, and a pool of backgrounds painted ahead of time.

  mock   card.procedural.stadium, drawn with numpy and Pillow from the style
         name and the seed. No weights, no GPU, about 200 ms.
  real   FLUX.2 [klein] 4B through diffusers, bf16, 4 steps. On a card of its
         own it takes 17,956 MiB. With ART_CPU_OFFLOAD=1, the one-GPU layout,
         its weights stay in host memory and stream to the card as it paints.

The request carries both `prompt` and `style`. The real backend paints the
prompt. The mock cannot read English, so it uses the style name to pick a
palette. The seed decides everything else, so the same request always gets the
same picture.

A request for a style's own recipe (card/backgrounds.py, which is what a card
asks for) is answered from the pool in art/pool.py when one is ready: a
background painted while nothing else wanted the model. X-Art-Source says
`pool`. Anything else, or `"fresh": true`, is painted on the spot and says
`fresh`.

Nothing here writes a letter on the image. Every word on the card is laid out in
HTML by the api.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

from fastapi import Depends, HTTPException
from fastapi.responses import Response
from PIL import Image
from pydantic import BaseModel, Field

from art.pool import Keeper, Pool, Turns, png_bytes, recipe_key
from card.backgrounds import recipe, styles
from card.procedural import stadium
from common import __version__
from common.auth import require_api_key
from common.meta import ART_SOURCE, model_headers
from common.schemas import STYLES
from common.service import ModelService, make_app
from common.settings import get_settings

log = logging.getLogger("art")

MOCK_MODEL = "mock/procedural-stadium"
MOCK_LICENSE = "n/a"
# Part of the pool's recipe hash. Bump it when card.procedural draws differently,
# so the mock's pool is painted again.
MOCK_REVISION = "mock-1"

REAL_MODEL = "black-forest-labs/FLUX.2-klein-4B"
REAL_LICENSE = "Apache-2.0"
REAL_REVISION = "e7b7dc27f91deacad38e78976d1f2b499d76a294"

# klein is a distilled model. It is sampled with the guidance off, at four
# steps, and both numbers come from the model card. The pipeline's own default
# is 50 steps at guidance 4.0, which is the setting for the undistilled model
# and produces a washed out image here.
GUIDANCE = 1.0

# The VAE downsamples by 8 and the transformer patches 2x2 on top of that, so
# the pipeline needs both sides divisible by 16. It only logs a warning and
# resizes behind your back, which would hand the compositor a frame that is not
# the size it asked for. `_snap` does the rounding out loud instead.
SIZE_MULTIPLE = 16

settings = get_settings()


class ArtRequest(BaseModel):
    """What the api sends. Sizes are capped so one request cannot fill the card."""

    prompt: str = Field(min_length=1, max_length=2000)
    # Not part of the prompt. It names the pool a style's recipe is taken
    # from, and the mock uses it to choose a palette.
    style: str = Field(default=STYLES[0], max_length=40)
    seed: int = Field(default=0, ge=0, le=2**31 - 1)
    width: int = Field(default=1024, ge=256, le=1536)
    height: int = Field(default=1408, ge=256, le=1536)
    steps: int = Field(default=4, ge=1, le=50)
    # Paint a new one even when the pool has one ready, with the seed asked for.
    fresh: bool = False


def _snap(value: int) -> int:
    """The nearest usable size at or below `value`. Never below one multiple."""
    return max(SIZE_MULTIPLE, value - value % SIZE_MULTIPLE)


class MockArt(ModelService):
    """The drawn ballpark. Deterministic in the seed."""

    revision = MOCK_REVISION

    def __init__(self, config) -> None:
        super().__init__(config, model=MOCK_MODEL, licence=MOCK_LICENSE)

    async def load(self) -> None:
        # A real warm-up, not a token one. stadium() draws bands and a crowd
        # speckle that need a frame with some height to it.
        await asyncio.to_thread(stadium, STYLES[0], 256, 352, 0)
        self.loaded = True

    def paint(self, request: ArtRequest, give_way: threading.Event | None = None) -> Image.Image:
        """About 200 ms, too short to stop halfway, so `give_way` is not read."""
        return stadium(request.style, request.width, request.height, seed=request.seed)


class RealArt(ModelService):
    """FLUX.2 [klein] 4B through diffusers. 16 GB of weights in the folders the
    pipeline reads: the text encoder 8.0 GB, the transformer 7.8 GB, the VAE
    0.2 GB.

    Seeds are reproducible for one device and one dtype. The same seed on a CPU,
    on a different card, or in fp16 gives a different picture. That is how
    sampling works, and it is why the card records which GPU painted it.
    """

    revision = REAL_REVISION

    def __init__(self, config) -> None:
        super().__init__(config, model=REAL_MODEL, licence=REAL_LICENSE)
        self._pipe = None

    async def load(self) -> None:
        # 16 GB off a disk and one warm-up generation. Both block for minutes,
        # and the event loop has a health check to answer, so they go to a thread.
        await asyncio.to_thread(self._load_blocking)
        self.loaded = True

    def _load_blocking(self) -> None:
        import torch
        from diffusers import Flux2KleinPipeline

        pipe = Flux2KleinPipeline.from_pretrained(
            REAL_MODEL, revision=REAL_REVISION, torch_dtype=torch.bfloat16
        )
        if self.settings.art_cpu_offload:
            # The one-GPU layout. The weights stay in host memory and each layer
            # is copied to the card when it runs, so the card holds one layer and
            # the activations, under 3 GB, beside vLLM and BiRefNet. Moving whole
            # submodels instead (enable_model_cpu_offload) still puts the 7.2 GiB
            # transformer on the card at once, and that plus Gemma 4 12B's 9.6 GiB
            # of weights is more than a 20 GB card holds before either has done
            # any work. Every painting copies the weights across again, so it is
            # slower. `.to()` must not be called as well: it would undo the
            # hooks by putting everything on the card anyway.
            pipe.enable_sequential_cpu_offload(device=self.settings.device)
        else:
            pipe.to(self.settings.device)
        # tqdm writes a progress bar per step into the container log.
        pipe.set_progress_bar_config(disable=True)
        self._pipe = pipe

        # A real warm-up, at the size a card asks for. Attention and VAE kernels
        # are chosen per shape, so a small warm-up would leave the first card
        # paying to build the ones it actually uses. One step is enough: steps
        # two through four run the same kernels on the same shapes, and this is
        # already the slowest part of starting the service.
        self.paint(ArtRequest(**{**recipe(styles()[0]), "steps": 1}))

    def paint(
        self, request: ArtRequest, give_way: threading.Event | None = None
    ) -> Image.Image | None:
        """The picture. None when `give_way` was set before the last step.

        The pool's painter passes `give_way`, and a request that wants the model
        sets it. diffusers calls `callback_on_step_end` after every denoising
        step, and setting `_interrupt` there skips the steps that are left. That
        is the pattern the diffusers docs give for stopping a pipeline early. The
        VAE still decodes what is there, and the half-made picture is dropped.
        """
        import torch

        width, height = _snap(request.width), _snap(request.height)
        # One generator per request, seeded here. Reusing the pipeline's own RNG
        # would make every image depend on how many came before it.
        generator = torch.Generator(device=self.settings.device).manual_seed(request.seed)

        stopped = False

        def between_steps(pipe, step: int, timestep, tensors: dict) -> dict:
            nonlocal stopped
            # After the last step the picture is finished. Keep it.
            if give_way.is_set() and step + 1 < request.steps:
                pipe._interrupt = True
                stopped = True
            return tensors

        extra = {"callback_on_step_end": between_steps} if give_way is not None else {}
        result = self._pipe(
            prompt=request.prompt,
            width=width,
            height=height,
            num_inference_steps=request.steps,
            guidance_scale=GUIDANCE,
            generator=generator,
            output_type="pil",
            **extra,
        )
        if stopped:
            return None
        image = result.images[0]

        if image.size != (request.width, request.height):
            # Only when the request was not a multiple of 16. A card's 1024x1408
            # is, so the card path never lands here.
            image = image.resize((request.width, request.height), Image.LANCZOS)
        return image


service: MockArt | RealArt = RealArt(settings) if settings.is_real else MockArt(settings)

# Every inference goes through here: a request first, the pool's painter when
# nobody asks. It holds the service's own lock.
turns = Turns(service.lock)

# The pool's painter. Made at startup once the model is loaded, and only when
# ART_POOL_SIZE is above 0.
keeper: Keeper | None = None


def pool_request(style: str, seed: int) -> ArtRequest:
    """What the painter paints for a style: the recipe a card asks for."""
    return ArtRequest(**recipe(style), seed=seed)


async def start() -> None:
    """Load the model, then fill the pool while nothing else wants it."""
    global keeper
    await service.startup()
    if not service.loaded or settings.art_pool_size < 1:
        return
    recipes = {style: recipe(style) for style in styles()}
    keys = {
        style: recipe_key(made, service.model, service.revision, service.ran_on)
        for style, made in recipes.items()
    }
    pool = Pool(settings.data_dir / "pool", settings.art_pool_size, recipes, keys)
    try:
        await asyncio.to_thread(pool.load)
    except OSError as exc:
        # A card still gets a background, painted when it asks.
        log.warning("no pool: %s is not usable. %s", pool.root, exc)
        return
    keeper = Keeper(pool, turns, service.paint, pool_request)
    keeper.start()
    log.info("pool: %d of %d ready in %s", pool.total(), pool.target(), pool.root)


async def stop() -> None:
    if keeper is not None:
        await keeper.stop()


app = make_app("baseball-card art", __version__, settings, on_start=start, on_stop=stop)


@app.get("/healthz", dependencies=[Depends(require_api_key)])
async def healthz() -> dict:
    """Model, backend, device, GPU, loaded, vram_mb, and how full the pool is:
    0 of 0 when it is off. The api copies these."""
    ready, size = (keeper.pool.total(), keeper.pool.target()) if keeper else (0, 0)
    return {**service.health(), "pool_ready": ready, "pool_size": size}


def png_response(data: bytes, infer_ms: float, source: str) -> Response:
    headers = model_headers(service.model, service.licence, infer_ms, service.ran_on)
    headers[ART_SOURCE] = source
    return Response(data, media_type="image/png", headers=headers)


async def from_pool(request: ArtRequest) -> Response | None:
    """A pooled background for a request that asks for a style's recipe. None
    when the request asks for something else, or the style has none ready."""
    if keeper is None or not keeper.pool.matches(request):
        return None
    entry = keeper.pool.take(request.style)
    keeper.poke()
    if entry is None:
        return None
    data = await asyncio.to_thread(Pool.read, entry)
    if data is None:
        return None
    # X-Infer-Ms is how long the painting took when it was painted. The api
    # times the step itself, which is what the card waited.
    return png_response(data, entry.ms, "pool")


@app.post("/art/generate", dependencies=[Depends(require_api_key)])
async def generate(request: ArtRequest) -> Response:
    """A PNG background at the requested size. No text, no people."""
    if not service.loaded:
        why = service.load_error or "still loading"
        raise HTTPException(503, f"the model is not loaded: {why}")

    if not request.fresh:
        pooled = await from_pool(request)
        if pooled is not None:
            return pooled

    started = time.perf_counter()
    async with turns.request():
        image = await asyncio.to_thread(service.paint, request)
    elapsed_ms = (time.perf_counter() - started) * 1000

    # In a thread: 2 MB of PNG takes half a second to write, and the event loop
    # has pooled backgrounds to hand out meanwhile.
    data = await asyncio.to_thread(png_bytes, image)
    return png_response(data, elapsed_ms, "fresh")
