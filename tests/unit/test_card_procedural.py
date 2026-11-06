"""The two stand-ins for the vision models, in `card/procedural.py`.

The matte is the one that matters. A hole in it puts a stadium through the
middle of a face, which is the one thing this demo promises cannot happen.
"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFilter

from card.procedural import plain_wall_cutout, stadium
from common.schemas import STYLES
from tests.conftest import REPO_ROOT

FIXTURES = REPO_ROOT / "tests" / "fixtures"
PORTRAIT = FIXTURES / "portrait.jpg"


def _photo_on_a_wall(size: tuple[int, int] = (300, 400)) -> Image.Image:
    """A dark oval on a light wall, with a hole in it the colour of the wall.

    The hole stands in for everything the colour distance gets wrong inside a
    subject: an eye, a lens, dark hair, the shadow under a chin.
    """
    photo = Image.new("RGB", size, (214, 210, 202))
    draw = ImageDraw.Draw(photo)
    draw.ellipse([60, 40, 240, 360], fill=(46, 38, 34))
    draw.ellipse([120, 120, 170, 170], fill=(212, 208, 200))
    return photo


# --------------------------------------------------------------- the matte


def test_the_matte_has_no_hole_in_it_even_where_the_photo_does():
    alpha = np.asarray(plain_wall_cutout(_photo_on_a_wall()).getchannel("A"))
    assert alpha[145, 145] == 255, "the wall-coloured patch inside the subject is opaque"
    assert alpha[10, 10] == 0, "the wall itself is not"


def test_the_matte_is_solid_everywhere_but_the_outer_pixel_or_two():
    """Feather the outside. Nothing else. Partial alpha inland is a hole."""
    channel = plain_wall_cutout(_photo_on_a_wall()).getchannel("A")
    alpha = np.asarray(channel)

    # Four pixels in from the silhouette, by a real erosion.
    solid = channel.point(lambda value: 255 if value > 250 else 0)
    inland = np.asarray(solid.filter(ImageFilter.MinFilter(9))) > 0

    assert inland.sum() > 10_000
    assert alpha[inland].min() == 255


def test_a_speck_of_shadow_in_a_corner_does_not_come_out_as_part_of_the_subject():
    photo = _photo_on_a_wall()
    ImageDraw.Draw(photo).ellipse([10, 340, 44, 380], fill=(52, 44, 40))

    alpha = np.asarray(plain_wall_cutout(photo).getchannel("A"))
    assert alpha[360, 27] == 0, "the speck is not joined to the body, so it goes"
    assert alpha[200, 150] == 255, "the body stays"


def test_the_matte_of_the_real_fixture_portrait_covers_the_person():
    cutout = plain_wall_cutout(Image.open(PORTRAIT))
    alpha = np.asarray(cutout.getchannel("A"))
    covered = (alpha > 250).mean()
    assert 0.2 < covered < 0.7, f"a headshot should be a good part of the frame, got {covered:.2f}"


def test_the_checked_in_cutout_is_what_the_matte_makes_of_the_portrait():
    """tests/fixtures/SOURCES.md says how cutout.png was made. That still holds."""
    with Image.open(PORTRAIT) as portrait, Image.open(FIXTURES / "cutout.png") as checked_in:
        made = plain_wall_cutout(portrait)
        assert made.tobytes() == checked_in.convert("RGBA").tobytes()


def test_the_same_photo_gives_the_same_matte():
    photo = _photo_on_a_wall()
    assert plain_wall_cutout(photo).tobytes() == plain_wall_cutout(photo).tobytes()


# ---------------------------------------------------------------- the art


@pytest.mark.parametrize("style", STYLES)
def test_every_style_draws_a_stadium_at_the_size_it_was_asked_for(style):
    assert stadium(style, 256, 352, seed=3).size == (256, 352)


def test_the_two_styles_draw_two_different_parks():
    """A night stadium for akamai and a day ballpark for classic, as their
    prompts ask FLUX for. At the size the art service paints, the sky between
    the floodlight towers is dark at night and light by day."""
    sky = (slice(0, 140), slice(400, 624))
    night = np.asarray(stadium("akamai", 1024, 1408, seed=5), dtype=float)[sky]
    day = np.asarray(stadium("classic", 1024, 1408, seed=5), dtype=float)[sky]
    assert night.mean() < 60 < 120 < day.mean()


def test_the_same_seed_draws_the_same_stadium():
    assert stadium("akamai", 128, 176, seed=5).tobytes() == (
        stadium("akamai", 128, 176, seed=5).tobytes()
    )


def test_a_different_seed_draws_a_different_stadium():
    assert stadium("akamai", 128, 176, seed=5).tobytes() != (
        stadium("akamai", 128, 176, seed=6).tobytes()
    )
