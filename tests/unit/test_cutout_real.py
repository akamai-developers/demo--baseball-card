"""The BiRefNet backend in `services/cutout`.

Almost everything here runs with the model call stubbed, so it needs no weights,
no torch and no GPU. What is being checked is the code around the model, which
is where the mistakes that survive a visual inspection live: the wrong
normalisation, the wrong output picked out of the list, a mask returned at the
model's resolution instead of the photo's, fp16 on a CPU.

The one test that does load the real weights is marked `slow`. `make test` skips
it. `make test-slow` runs it on this machine's CPU.
"""

from __future__ import annotations

import sys
import types

import numpy as np
import pytest
from PIL import Image

from card.matte import as_mask, shrink
from cutout.main import (
    IMAGENET_MEAN,
    IMAGENET_STD,
    INPUT_PX,
    REAL_REVISION,
    RealCutout,
)
from tests.conftest import TEST_KEY


class FakeTensor:
    """Enough of a torch tensor for the four calls `cut` makes on the result."""

    def __init__(self, array: np.ndarray) -> None:
        self.array = array
        self.moved_to: dict | None = None

    # -- what the model returns
    def sigmoid(self) -> FakeTensor:
        return FakeTensor(1.0 / (1.0 + np.exp(-self.array)))

    def float(self) -> FakeTensor:
        return FakeTensor(self.array.astype(np.float32))

    def cpu(self) -> FakeTensor:
        return self

    def numpy(self) -> np.ndarray:
        return self.array

    def __getitem__(self, index):
        return FakeTensor(self.array[index])

    # -- what _preprocess does to the input
    def unsqueeze(self, axis: int) -> FakeTensor:
        return FakeTensor(np.expand_dims(self.array, axis))

    def to(self, **kwargs) -> FakeTensor:
        self.moved_to = kwargs
        return self


class FakeTorch(types.ModuleType):
    """A stand-in for `import torch` inside cut() and _preprocess()."""

    float16 = "float16"
    float32 = "float32"

    def __init__(self) -> None:
        super().__init__("torch")
        self.inference_mode_entered = 0

    def from_numpy(self, array: np.ndarray) -> FakeTensor:
        return FakeTensor(array)

    def inference_mode(self):
        outer = self

        class Guard:
            def __enter__(self):
                outer.inference_mode_entered += 1

            def __exit__(self, *exc):
                return False

        return Guard()


class FakeModel:
    """Returns a stack of predictions, coarse to fine, like the real decoder."""

    def __init__(self, mask: np.ndarray | None = None) -> None:
        self.mask = mask
        self.calls: list[FakeTensor] = []

    def __call__(self, batch: FakeTensor) -> list[FakeTensor]:
        self.calls.append(batch)
        fine = self.mask
        if fine is None:
            fine = np.zeros((INPUT_PX, INPUT_PX), dtype=np.float32)
            fine[200:800, 300:700] = 1.0
        # Logits, not probabilities: cut() is the thing that applies the sigmoid.
        logits = np.where(fine > 0.5, 8.0, -8.0).astype(np.float32)
        wrong = np.full((1, 1, 64, 64), -8.0, dtype=np.float32)
        return [FakeTensor(wrong), FakeTensor(logits[None, None, :, :])]


@pytest.fixture
def stubbed(clean_settings, monkeypatch):
    """A RealCutout whose model is a fake and whose torch is a fake."""
    monkeypatch.setitem(sys.modules, "torch", FakeTorch())
    service = RealCutout(clean_settings)
    service._model = FakeModel()
    service._dtype = "float32"
    return service


def photo(size: tuple[int, int] = (800, 600)) -> Image.Image:
    rng = np.random.default_rng(0)
    return Image.fromarray(rng.integers(0, 255, (size[1], size[0], 3), dtype=np.uint8), "RGB")


# ---------------------------------------------------------------- preprocess


def test_the_photo_is_squared_off_to_the_size_the_weights_expect(stubbed):
    stubbed.cut(photo((800, 600)))
    batch = stubbed._model.calls[0].array
    assert batch.shape == (1, 3, INPUT_PX, INPUT_PX)


def test_the_photo_is_normalised_the_way_imagenet_models_expect(stubbed):
    """Skip the normalisation and the matte is confident nonsense, not an error."""
    flat = Image.new("RGB", (64, 64), (255, 255, 255))
    stubbed.cut(flat)
    batch = stubbed._model.calls[0].array

    expected = [(1.0 - mean) / std for mean, std in zip(IMAGENET_MEAN, IMAGENET_STD, strict=True)]
    for channel, value in enumerate(expected):
        assert batch[0, channel] == pytest.approx(value, abs=1e-4)


def test_channels_come_before_rows(stubbed):
    """A transposed batch runs, produces a mask, and is wrong in a way nothing catches."""
    red = Image.new("RGB", (64, 64), (255, 0, 0))
    stubbed.cut(red)
    batch = stubbed._model.calls[0].array
    assert batch[0, 0].min() > batch[0, 1].max(), "the red channel is not first"


@pytest.mark.parametrize(
    ("device", "dtype"),
    [("cuda", "float16"), ("cpu", "float32")],
)
def test_the_batch_lands_on_the_device_and_dtype_the_settings_asked_for(
    clean_settings, monkeypatch, device, dtype
):
    """fp16 on a CPU is not a slow path, it is a missing kernel."""
    from common.settings import Settings

    monkeypatch.setitem(sys.modules, "torch", FakeTorch())
    service = RealCutout(Settings(device=device, card_api_key=TEST_KEY))
    service._model = FakeModel()
    service._dtype = dtype

    service.cut(photo((128, 128)))
    assert service._model.calls[0].moved_to == {"device": device, "dtype": dtype}


# ------------------------------------------------------------------ the mask


def test_the_finest_prediction_is_the_one_used(stubbed):
    """forward() returns the decoder coarse to fine. Taking [0] gives a 64 px guess."""
    alpha = np.asarray(stubbed.cut(photo((256, 256))).getchannel("A"))
    assert alpha.max() == 255, "the mask is empty, so the coarse prediction was used"


def test_the_prediction_is_run_through_a_sigmoid(stubbed):
    """The head emits logits. Scaling them straight to bytes clips to 0 and 255."""
    smooth = np.full((INPUT_PX, INPUT_PX), 0.0, dtype=np.float32)
    smooth[400:600, 400:600] = 1.0
    stubbed._model = FakeModel(smooth)
    out = stubbed.cut(photo((512, 512)))
    assert np.asarray(out.getchannel("A")).max() == 255


def test_the_cutout_is_the_size_of_the_photo_and_not_the_size_of_the_model(stubbed):
    for size in ((800, 600), (600, 800), (1024, 683)):
        out = stubbed.cut(photo(size))
        assert out.size == size
        assert out.mode == "RGBA"


def test_the_colours_are_the_photo_untouched(stubbed):
    """The model decides what is transparent. It does not get to repaint a face."""
    source = photo((300, 300))
    out = stubbed.cut(source)
    assert np.array_equal(np.asarray(out.convert("RGB")), np.asarray(source))


def test_a_speck_the_model_was_sure_about_is_not_a_subject(stubbed):
    mask = np.zeros((INPUT_PX, INPUT_PX), dtype=np.float32)
    mask[200:800, 300:700] = 1.0
    mask[10:20, 10:20] = 1.0
    stubbed._model = FakeModel(mask)

    alpha = np.asarray(stubbed.cut(photo((INPUT_PX, INPUT_PX))).getchannel("A"))
    assert alpha[5:25, 5:25].max() == 0, "a speck survived refine_matte"


def test_a_hole_the_model_left_in_the_subject_is_closed(stubbed):
    mask = np.zeros((INPUT_PX, INPUT_PX), dtype=np.float32)
    mask[200:800, 300:700] = 1.0
    mask[480:520, 480:520] = 0.0
    stubbed._model = FakeModel(mask)

    alpha = np.asarray(stubbed.cut(photo((INPUT_PX, INPUT_PX))).getchannel("A"))
    assert alpha[500, 500] == 255, "a hole in the subject stayed transparent"


def test_nothing_is_asked_of_the_model_outside_inference_mode(stubbed):
    """Autograd on a 1024 px forward is gigabytes of graph nobody reads."""
    stubbed.cut(photo((256, 256)))
    assert sys.modules["torch"].inference_mode_entered == 1


# ----------------------------------------------------------------- the model


def test_the_revision_is_pinned_to_a_commit_and_not_a_branch():
    """trust_remote_code=True runs birefnet.py off the Hub. main is not a pin."""
    assert len(REAL_REVISION) == 40
    assert set(REAL_REVISION) <= set("0123456789abcdef")


def test_load_pins_the_revision_and_turns_training_off(clean_settings, monkeypatch):
    seen = {}

    class Loader:
        @staticmethod
        def from_pretrained(name, **kwargs):
            seen["name"] = name
            seen.update(kwargs)

            class Loaded:
                def eval(self):
                    seen["eval"] = True

                def to(self, **kw):
                    seen["to"] = kw

            return Loaded()

    monkeypatch.setitem(sys.modules, "torch", FakeTorch())
    monkeypatch.setitem(
        sys.modules, "transformers", types.SimpleNamespace(AutoModelForImageSegmentation=Loader)
    )
    service = RealCutout(clean_settings)
    monkeypatch.setattr(service, "cut", lambda image: image)  # skip the warm-up
    service._load_blocking()

    assert seen["name"] == "ZhengPeng7/BiRefNet"
    assert seen["revision"] == REAL_REVISION
    assert seen["trust_remote_code"] is True
    assert seen["eval"] is True
    assert seen["to"] == {"device": "cpu", "dtype": "float32"}


def test_load_runs_a_warm_up_on_something_that_has_a_subject_in_it(clean_settings, monkeypatch):
    """A warm-up on a blank frame skips the decoder and proves nothing."""
    warmed: list[Image.Image] = []

    class Loader:
        @staticmethod
        def from_pretrained(name, **kwargs):
            return types.SimpleNamespace(eval=lambda: None, to=lambda **kw: None)

    monkeypatch.setitem(sys.modules, "torch", FakeTorch())
    monkeypatch.setitem(
        sys.modules, "transformers", types.SimpleNamespace(AutoModelForImageSegmentation=Loader)
    )
    service = RealCutout(clean_settings)
    monkeypatch.setattr(service, "cut", lambda image: warmed.append(image) or image)
    service._load_blocking()

    assert len(warmed) == 1
    frame = np.asarray(warmed[0].convert("L"))
    assert len(np.unique(frame)) > 2, "the warm-up frame is flat"


# ------------------------------------------------------- the real weights, CPU


@pytest.mark.slow
def test_birefnet_on_this_cpu_covers_the_person_in_the_fixture(clean_settings, portrait):
    """The real model, the real weights, the real photo, on a CPU.

    Nothing here asserts a pretty matte: that is for a person looking at a
    card. What it asserts is that the plumbing is right: the person is opaque,
    the corners are not, and the edge is soft rather than cut with scissors.
    """
    pytest.importorskip("torch", reason="run: make test-slow")
    pytest.importorskip("transformers", reason="run: make test-slow")

    import asyncio

    clean_settings.backend = "real"
    service = RealCutout(clean_settings)
    asyncio.run(service.load())
    assert service.loaded

    with Image.open(portrait) as opened:
        source = opened.convert("RGB")
    out = service.cut(source)

    assert out.size == source.size
    alpha = np.asarray(out.getchannel("A"))
    height, width = alpha.shape

    covered = (alpha > 127).mean()
    assert 0.15 < covered < 0.60, (
        f"the subject is {covered:.0%} of the frame, which is not a person"
    )

    # No hole anywhere inside the subject. This is the property the demo sells,
    # and it does not need to know where the face is to check it. 12 px in from
    # the silhouette is well past the feather.
    solid = alpha > 127
    interior = alpha[np.asarray(shrink(as_mask(solid), 12.0)) > 127]
    assert interior.min() > 128, "the background shows through the middle of the subject"
    assert (interior == 255).mean() > 0.999, "the matte is unsure about the inside of the subject"

    # The head, found from the subject rather than from a guess at where a face
    # goes. A tenth of the way down from the top of the silhouette is forehead
    # on this fixture, and it comes out as one unbroken run 122 px wide.
    top = int(np.nonzero(solid.any(axis=1))[0][0])
    columns = np.nonzero(solid[top + int(height * 0.10)])[0]
    assert columns.size > width * 0.08, "the head is too narrow to be a head"
    assert int(np.diff(columns).max()) == 1, "the matte has a gap across the face"

    # The corners are wall in this photo, and none of them is the subject.
    for row, column in ((0, 0), (0, width - 1), (height - 1, 0), (height - 1, width - 1)):
        assert alpha[row, column] == 0, f"the wall at ({row}, {column}) came out as subject"

    soft = ((alpha > 8) & (alpha < 247)).mean()
    assert soft > 0.001, "every pixel is opaque or clear, so the matte was hard thresholded"
