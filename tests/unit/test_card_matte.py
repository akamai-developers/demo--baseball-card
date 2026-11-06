"""The mask arithmetic in `card/matte.py`.

Every matte in the build goes through these, the BiRefNet one included, so a bug
here shows up as a hole in a face.
"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image, ImageDraw

from card.matte import (
    as_mask,
    drop_small_islands,
    fill_holes,
    grow,
    largest_blob,
    refine_matte,
    shrink,
)


def soft_blob(size=(80, 80), box=(20, 20, 60, 60), edge: int = 96) -> Image.Image:
    """A solid blob with a ring of not-quite-sure alpha around it.

    The ring stands in for a hairline: the part of a model's matte that is worth
    keeping and that a hard threshold throws away. 96 is below the 0.5 the refine
    calls subject, so it has to survive on its own rather than by being rounded up.
    """
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    left, top, right, bottom = box
    draw.ellipse([left - 2, top - 2, right + 2, bottom + 2], fill=edge)
    draw.ellipse([left, top, right, bottom], fill=255)
    return mask


# ------------------------------------------------------------------ fill_holes


def test_fill_holes_closes_what_the_border_cannot_reach():
    mask = np.zeros((40, 40), dtype=bool)
    mask[8:32, 8:32] = True
    mask[16:24, 16:24] = False
    assert fill_holes(mask)[16:24, 16:24].all()


def test_fill_holes_works_when_the_subject_runs_off_every_edge():
    """The bounding-box crop guarantees this, and seeding from a corner does not."""
    mask = np.ones((40, 40), dtype=bool)
    mask[16:24, 16:24] = False
    assert fill_holes(mask).all()


def test_fill_holes_leaves_a_notch_open_because_the_border_does_reach_it():
    mask = np.zeros((40, 40), dtype=bool)
    mask[8:32, 8:32] = True
    mask[16:24, 8:16] = False
    assert not fill_holes(mask)[16:24, 8:16].any()


def _flood_filled(binary: np.ndarray) -> np.ndarray:
    """What fill_holes replaced: a flood fill of the background from outside
    the frame. Right, and a second per phone photo."""
    height, width = binary.shape
    outside = Image.new("L", (width + 2, height + 2), 255)
    outside.paste(Image.fromarray(np.where(binary, 0, 255).astype(np.uint8), "L"), (1, 1))
    ImageDraw.floodfill(outside, (0, 0), 0)
    return binary | (np.asarray(outside)[1 : height + 1, 1 : width + 1] > 0)


def test_fill_holes_matches_the_flood_fill_it_replaced():
    """Speckled masks have hundreds of pieces of background, some joined only at
    a corner, which a flood fill does not cross. Every one must agree."""
    rng = np.random.default_rng(20)
    for _ in range(200):
        height, width = (int(n) for n in rng.integers(1, 48, size=2))
        mask = rng.random((height, width)) < rng.uniform(0.2, 0.8)
        assert (fill_holes(mask) == _flood_filled(mask)).all()


def test_fill_holes_of_a_frame_with_no_background_or_no_subject():
    assert fill_holes(np.ones((5, 7), dtype=bool)).all()
    assert not fill_holes(np.zeros((5, 7), dtype=bool)).any()


# ---------------------------------------------------------------- the islands


def test_largest_blob_keeps_the_body_and_drops_everything_else():
    mask = np.zeros((60, 60), dtype=bool)
    mask[10:40, 10:40] = True
    mask[50:56, 50:56] = True
    mask[2, 2] = True

    kept = largest_blob(mask)
    assert kept[10:40, 10:40].all()
    assert not kept[50:56, 50:56].any()
    assert not kept[2, 2]


def test_largest_blob_joins_pieces_that_touch_through_a_narrow_neck():
    mask = np.zeros((40, 40), dtype=bool)
    mask[4:16, 4:36] = True
    mask[16:20, 18:22] = True
    mask[20:36, 4:36] = True
    assert largest_blob(mask).sum() == mask.sum()


def test_largest_blob_of_an_empty_mask_is_an_empty_mask():
    assert not largest_blob(np.zeros((10, 10), dtype=bool)).any()


def test_drop_small_islands_keeps_a_hand_that_came_away_from_the_body():
    """The reason this is not largest_blob. An arm held out is its own component."""
    mask = np.zeros((100, 100), dtype=bool)
    mask[10:90, 30:70] = True  # the body, 3200 px
    mask[40:55, 78:93] = True  # a hand, 225 px, 7 percent of it
    mask[5, 5] = True  # a speck

    kept = drop_small_islands(mask, 0.05)
    assert kept[40:55, 78:93].all(), "a hand at 7 percent of the body was thrown away"
    assert not kept[5, 5]


def test_drop_small_islands_at_a_high_fraction_is_largest_blob():
    mask = np.zeros((100, 100), dtype=bool)
    mask[10:90, 30:70] = True
    mask[40:55, 78:93] = True
    assert (drop_small_islands(mask, 1.0) == largest_blob(mask)).all()


def test_drop_small_islands_of_an_empty_mask_is_an_empty_mask():
    assert not drop_small_islands(np.zeros((10, 10), dtype=bool)).any()


# ------------------------------------------------------------- grow and shrink


def test_grow_and_shrink_move_the_edge_in_the_directions_they_say():
    mask = as_mask(np.pad(np.ones((20, 20), dtype=bool), 20))
    before = np.asarray(mask).sum()
    assert np.asarray(grow(mask, 3.0)).sum() > before
    assert np.asarray(shrink(mask, 3.0)).sum() < before


def test_shrink_then_grow_leaves_a_shape_about_where_it_started():
    """A closing must not walk the silhouette across the frame."""
    mask = as_mask(np.pad(np.ones((40, 40), dtype=bool), 20))
    closed = np.asarray(grow(shrink(mask, 3.0), 3.0), dtype=float) / 255
    assert abs(closed.sum() - 40 * 40) < 40 * 40 * 0.25


# --------------------------------------------------------------- refine_matte


def test_refine_fills_a_hole_and_makes_it_opaque():
    mask = soft_blob()
    ImageDraw.Draw(mask).ellipse([35, 35, 45, 45], fill=0)
    refined = np.asarray(refine_matte(mask))
    assert refined[40, 40] == 255, "a hole inside the subject stayed transparent"


def test_refine_forces_a_half_sure_interior_opaque():
    """Alpha 0.6 on a cheek is a stadium showing through a face at 40 percent."""
    mask = soft_blob()
    ImageDraw.Draw(mask).ellipse([30, 30, 50, 50], fill=150)
    assert np.asarray(refine_matte(mask))[40, 40] == 255


def test_refine_drops_a_speck_and_its_halo_with_it():
    mask = soft_blob()
    ImageDraw.Draw(mask).ellipse([4, 4, 9, 9], fill=255)
    refined = np.asarray(refine_matte(mask))
    assert refined[4:11, 4:11].max() == 0, "a dropped island left a grey smudge behind"


def test_refine_keeps_the_soft_edge_instead_of_cutting_it_with_scissors():
    """The half transparent pixels along a hairline are the model's best work."""
    refined = np.asarray(refine_matte(soft_blob(), feather_px=0))
    edge = refined[(refined > 0) & (refined < 255)]
    assert edge.size > 0, "every pixel is 0 or 255, so the matte was hard thresholded"


def test_refine_of_an_empty_mask_is_an_empty_mask():
    """The model found nothing. There is no honest way to invent a subject."""
    empty = Image.new("L", (40, 40), 0)
    assert np.asarray(refine_matte(empty)).max() == 0


def test_refine_does_not_move_the_outline_more_than_a_couple_of_pixels():
    mask = soft_blob(box=(20, 20, 60, 60))
    refined = refine_matte(mask)
    before = Image.eval(mask, lambda v: 255 if v > 8 else 0).getbbox()
    after = Image.eval(refined, lambda v: 255 if v > 8 else 0).getbbox()
    assert max(abs(a - b) for a, b in zip(after, before, strict=True)) <= 3


@pytest.mark.parametrize("size", [(64, 48), (48, 64), (101, 97)])
def test_refine_returns_a_mask_the_same_size_it_was_given(size):
    assert refine_matte(soft_blob(size, box=(10, 10, 30, 30))).size == size
