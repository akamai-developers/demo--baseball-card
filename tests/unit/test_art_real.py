"""The FLUX.2 [klein] backend in `services/art`.

Everything here runs with the pipeline stubbed, so it needs no weights, no
diffusers and no GPU. There is no slow variant of this file the way there is for
the cutout service: four steps of a 4B model on a laptop's CPU is minutes per
image and 16 GB of weights, so the real path runs first on a GPU node.

What is checked is the code around the model, which is where the mistakes that
survive a visual inspection live: the pipeline's own defaults left in place
(50 steps at guidance 4.0, which is the undistilled model's recipe), a seed that
does not reach the sampler, a size the VAE quietly rounds off, fp16 instead of
bf16.
"""

from __future__ import annotations

import sys
import threading
import types

import pytest
from PIL import Image

from art.main import (
    GUIDANCE,
    REAL_MODEL,
    REAL_REVISION,
    SIZE_MULTIPLE,
    ArtRequest,
    RealArt,
    _snap,
)
from card.backgrounds import ART_H, ART_W, recipe, styles


class FakeGenerator:
    """torch.Generator. It only has to remember what it was seeded with."""

    def __init__(self, device: str) -> None:
        self.device = device
        self.seed: int | None = None

    def manual_seed(self, seed: int) -> FakeGenerator:
        self.seed = seed
        return self


class FakeTorch(types.ModuleType):
    """A stand-in for `import torch` inside _load_blocking() and paint()."""

    bfloat16 = "bfloat16"
    float16 = "float16"

    def __init__(self) -> None:
        super().__init__("torch")
        self.generators: list[FakeGenerator] = []

    def Generator(self, device: str) -> FakeGenerator:  # noqa: N802 - torch spells it this way
        made = FakeGenerator(device)
        self.generators.append(made)
        return made


class FakePipe:
    """Enough of Flux2KleinPipeline to record what the service asked it for."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.moved_to: str | None = None
        self.offloaded_to: str | None = None
        self.whole_parts_offloaded = False
        self.progress_bar_disabled: bool | None = None

    def to(self, device: str) -> FakePipe:
        self.moved_to = device
        return self

    def enable_sequential_cpu_offload(self, device: str | None = None) -> None:
        self.offloaded_to = device

    def enable_model_cpu_offload(self, device: str | None = None) -> None:
        self.whole_parts_offloaded = True

    def set_progress_bar_config(self, **kwargs) -> None:
        self.progress_bar_disabled = kwargs.get("disable")

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        # The real pipeline returns the size it was given, which is why the
        # service has to hand it a multiple of 16 rather than hope.
        image = Image.new("RGB", (kwargs["width"], kwargs["height"]), (20, 40, 80))
        return types.SimpleNamespace(images=[image])


def fake_diffusers(pipe: FakePipe, seen: dict) -> types.ModuleType:
    """A `diffusers` module whose from_pretrained records its arguments."""

    class Flux2KleinPipeline:
        @staticmethod
        def from_pretrained(name, **kwargs):
            seen["name"] = name
            seen.update(kwargs)
            return pipe

    return types.SimpleNamespace(Flux2KleinPipeline=Flux2KleinPipeline)


@pytest.fixture
def stubbed(clean_settings, monkeypatch):
    """A RealArt whose pipeline is a fake and whose torch is a fake."""
    monkeypatch.setitem(sys.modules, "torch", FakeTorch())
    service = RealArt(clean_settings)
    service._pipe = FakePipe()
    return service


def loaded(settings, monkeypatch) -> tuple[RealArt, FakePipe, dict]:
    """Run _load_blocking() against fakes and hand back what it touched."""
    pipe, seen = FakePipe(), {}
    monkeypatch.setitem(sys.modules, "torch", FakeTorch())
    monkeypatch.setitem(sys.modules, "diffusers", fake_diffusers(pipe, seen))
    service = RealArt(settings)
    service._load_blocking()
    return service, pipe, seen


# ------------------------------------------------------------------ the model


def test_the_revision_is_pinned_to_a_commit_and_not_a_branch():
    """16 GB of weights that can change under you is not a reproducible demo."""
    assert len(REAL_REVISION) == 40
    assert set(REAL_REVISION) <= set("0123456789abcdef")


def test_load_pins_the_revision_and_asks_for_bfloat16(clean_settings, monkeypatch):
    """fp16 overflows on this architecture. The model card says bf16."""
    _, _, seen = loaded(clean_settings, monkeypatch)

    assert seen["name"] == REAL_MODEL == "black-forest-labs/FLUX.2-klein-4B"
    assert seen["revision"] == REAL_REVISION
    assert seen["torch_dtype"] == "bfloat16"


def test_load_puts_the_weights_on_the_device_and_turns_the_progress_bar_off(
    clean_settings, monkeypatch
):
    """tqdm writes one bar per step into the container log."""
    _, pipe, _ = loaded(clean_settings, monkeypatch)

    assert pipe.moved_to == "cpu"  # clean_settings is a laptop
    assert pipe.offloaded_to is None
    assert pipe.progress_bar_disabled is True


def test_cpu_offload_replaces_the_move_rather_than_joining_it(clean_settings, monkeypatch):
    """.to() after the offload puts it all on the card anyway."""
    clean_settings.art_cpu_offload = True
    clean_settings.device = "cuda"
    _, pipe, _ = loaded(clean_settings, monkeypatch)

    assert pipe.offloaded_to == "cuda"
    assert pipe.moved_to is None, "the offload hooks were undone by a .to()"


def test_cpu_offload_goes_layer_by_layer(clean_settings, monkeypatch):
    """The one-GPU layout. Moved one whole part at a time, the transformer's
    7,392 MiB is on the card at once, and beside BiRefNet that leaves vLLM less
    than Gemma's weights. docker-compose.yml has the numbers."""
    clean_settings.art_cpu_offload = True
    clean_settings.device = "cuda"
    _, pipe, _ = loaded(clean_settings, monkeypatch)

    assert pipe.offloaded_to == "cuda"
    assert not pipe.whole_parts_offloaded


def test_load_warms_up_with_a_real_generation_at_the_size_a_card_asks_for(
    clean_settings, monkeypatch
):
    """Attention and VAE kernels are built per shape. A 256 px warm-up warms nothing."""
    _, pipe, _ = loaded(clean_settings, monkeypatch)

    assert len(pipe.calls) == 1, "the model was loaded without a warm-up"
    assert (pipe.calls[0]["width"], pipe.calls[0]["height"]) == (ART_W, ART_H)
    assert pipe.calls[0]["prompt"] == recipe(styles()[0])["prompt"]
    # Steps two to four run the same kernels on the same shapes.
    assert pipe.calls[0]["num_inference_steps"] == 1


def test_load_is_what_sets_loaded(clean_settings, monkeypatch):
    """The node is only ready when loaded is true, so it must mean the weights are in."""
    import asyncio

    pipe, seen = FakePipe(), {}
    monkeypatch.setitem(sys.modules, "torch", FakeTorch())
    monkeypatch.setitem(sys.modules, "diffusers", fake_diffusers(pipe, seen))
    service = RealArt(clean_settings)

    assert service.loaded is False
    asyncio.run(service.load())
    assert service.loaded is True


# ------------------------------------------------------------------ sampling


def test_klein_is_sampled_with_the_guidance_off(stubbed):
    """The pipeline defaults to 4.0, which is the undistilled model's recipe."""
    assert GUIDANCE == 1.0
    stubbed.paint(ArtRequest(prompt="a stadium"))
    assert stubbed._pipe.calls[0]["guidance_scale"] == 1.0


def test_the_step_count_comes_from_the_request_and_not_from_diffusers(stubbed):
    """The pipeline's default is 50 steps. On a distilled model that is 12x the bill."""
    stubbed.paint(ArtRequest(prompt="a stadium", steps=4))
    assert stubbed._pipe.calls[0]["num_inference_steps"] == 4


def test_the_prompt_is_passed_through_untouched(stubbed):
    """The api builds the prompt. The service does not get a second opinion."""
    prompt = "an empty baseball stadium at night, no text, no letters, no logos, no people"
    stubbed.paint(ArtRequest(prompt=prompt))
    assert stubbed._pipe.calls[0]["prompt"] == prompt


def test_a_pil_image_is_what_is_asked_for(stubbed):
    """The default is a tensor in some diffusers versions, and Response wants bytes."""
    stubbed.paint(ArtRequest(prompt="a stadium"))
    assert stubbed._pipe.calls[0]["output_type"] == "pil"


# ---------------------------------------------------------------- the seed


def test_the_seed_reaches_the_sampler(stubbed):
    stubbed.paint(ArtRequest(prompt="a stadium", seed=1234))
    generator = stubbed._pipe.calls[0]["generator"]
    assert generator.seed == 1234


def test_the_same_seed_seeds_the_same_way_twice(stubbed):
    """The same card id in the same style asks for the same seed, and gets the
    same background when it is painted fresh."""
    stubbed.paint(ArtRequest(prompt="a stadium", seed=77))
    stubbed.paint(ArtRequest(prompt="a stadium", seed=77))
    first, second = (call["generator"].seed for call in stubbed._pipe.calls)
    assert first == second == 77


def test_a_different_seed_is_a_different_generator(stubbed):
    """One generator per request. A shared one makes each image depend on the last."""
    stubbed.paint(ArtRequest(prompt="a stadium", seed=1))
    stubbed.paint(ArtRequest(prompt="a stadium", seed=2))
    first, second = (call["generator"] for call in stubbed._pipe.calls)
    assert first is not second
    assert (first.seed, second.seed) == (1, 2)


def test_the_generator_lives_on_the_same_device_as_the_weights(clean_settings, monkeypatch):
    """A CPU generator with CUDA latents is an error, not a slow path."""
    monkeypatch.setitem(sys.modules, "torch", FakeTorch())
    clean_settings.device = "cuda"
    service = RealArt(clean_settings)
    service._pipe = FakePipe()

    service.paint(ArtRequest(prompt="a stadium", seed=5))
    assert service._pipe.calls[0]["generator"].device == "cuda"


# ------------------------------------------------------- giving way to a card


class SteppingPipe(FakePipe):
    """Runs the step callback the way diffusers 0.40's Flux2KleinPipeline does:
    after every step, with the pipeline, the step index, the timestep and the
    tensors, and skips every step after one that set `_interrupt`. It pops
    "latents" out of what the callback returns, so the callback has to return
    the dict. Read in the 0.40.0 wheel's pipeline_flux2_klein.py, the denoising
    loop, on 2026-09-24.

    `set_at` sets `flag` just before the callback of that step runs, which is a
    card arriving while the painting is on that step.
    """

    def __init__(self, flag: threading.Event | None = None, set_at: int | None = None) -> None:
        super().__init__()
        self.flag = flag
        self.set_at = set_at
        self.ran = 0

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        self._interrupt = False
        self.ran = 0
        callback = kwargs.get("callback_on_step_end")
        for step in range(kwargs["num_inference_steps"]):
            if self._interrupt:
                continue
            self.ran += 1
            if self.flag is not None and step == self.set_at:
                self.flag.set()
            if callback is not None:
                tensors = {"latents": object()}
                assert callback(self, step, 1000 - step, tensors) is tensors
        image = Image.new("RGB", (kwargs["width"], kwargs["height"]), (20, 40, 80))
        return types.SimpleNamespace(images=[image])


def stepping(clean_settings, monkeypatch, pipe: SteppingPipe) -> RealArt:
    monkeypatch.setitem(sys.modules, "torch", FakeTorch())
    service = RealArt(clean_settings)
    service._pipe = pipe
    return service


def test_a_request_paints_the_whole_way_with_no_callback(clean_settings, monkeypatch):
    """Only the pool's painter gives way. A card's own painting runs to the end."""
    pipe = SteppingPipe()
    out = stepping(clean_settings, monkeypatch, pipe).paint(ArtRequest(prompt="a stadium"))
    assert "callback_on_step_end" not in pipe.calls[0]
    assert pipe.ran == 4
    assert out is not None


def test_a_painting_with_nobody_waiting_is_kept(clean_settings, monkeypatch):
    flag = threading.Event()
    pipe = SteppingPipe()
    out = stepping(clean_settings, monkeypatch, pipe).paint(ArtRequest(prompt="a stadium"), flag)
    assert pipe.ran == 4
    assert out is not None and out.size == (1024, 1408)


@pytest.mark.parametrize("step", [0, 1, 2])
def test_a_card_that_arrives_mid_painting_stops_it_after_that_step(
    clean_settings, monkeypatch, step
):
    """The card waits for one step and the VAE, not for the painting."""
    flag = threading.Event()
    pipe = SteppingPipe(flag, set_at=step)
    service = stepping(clean_settings, monkeypatch, pipe)

    out = service.paint(ArtRequest(prompt="a stadium", steps=4), flag)

    assert out is None, "a half-made picture would have gone into the pool"
    assert pipe.ran == step + 1


def test_a_card_that_arrives_on_the_last_step_does_not_waste_a_finished_picture(
    clean_settings, monkeypatch
):
    flag = threading.Event()
    pipe = SteppingPipe(flag, set_at=3)
    out = stepping(clean_settings, monkeypatch, pipe).paint(
        ArtRequest(prompt="a stadium", steps=4), flag
    )
    assert pipe.ran == 4
    assert out is not None


# ------------------------------------------------------------------- sizes


def test_snap_rounds_down_to_something_the_vae_can_divide():
    assert SIZE_MULTIPLE == 16
    assert _snap(1024) == 1024
    assert _snap(1408) == 1408
    assert _snap(1000) == 992
    assert _snap(257) == 256
    assert _snap(8) == SIZE_MULTIPLE, "a size below one multiple is not a size"


def test_the_card_size_goes_to_the_model_exactly_as_asked(stubbed):
    """A card's 1024x1408 divides by 16, so the card path never resizes anything."""
    out = stubbed.paint(ArtRequest(prompt="a stadium", width=1024, height=1408))
    call = stubbed._pipe.calls[0]
    assert (call["width"], call["height"]) == (1024, 1408)
    assert out.size == (1024, 1408)


def test_an_awkward_size_is_snapped_for_the_model_and_returned_as_requested(stubbed):
    """diffusers only logs a warning and resizes behind your back."""
    out = stubbed.paint(ArtRequest(prompt="a stadium", width=1000, height=1400))
    call = stubbed._pipe.calls[0]
    assert (call["width"], call["height"]) == (992, 1392)
    assert out.size == (1000, 1400), "the caller got a size it did not ask for"
