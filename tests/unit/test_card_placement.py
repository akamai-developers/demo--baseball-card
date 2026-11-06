"""The face rule in `card/placement.py`, as arithmetic.

place() takes sizes and boxes, so these build none of the pictures. Where a
test needs the frame the rule's numbers were first measured on, it is FRAME,
1412 x 2012. The card's own picture, above the name plate, is PICTURE.
"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image, ImageDraw

from api.qa import (
    FACE_LARGE,
    FACE_SMALL,
    LARGE_FACE,
    PLACED_HIGH,
    PLACED_LOW,
    SMALL_FACE,
    TIGHT,
    check_spot,
    placed_high,
    placed_low,
)
from card import PICTURE_H, PICTURE_W
from card.placement import (
    DEFAULT_PLACEMENT,
    FACE_H,
    FACE_MOST,
    FACE_Y,
    HEAD_TOP,
    MAX_SCALE,
    Placement,
    Spot,
    crown,
    cut_lines,
    estimate_face,
    face_on,
    place,
    shift_face,
)

FRAME = (1412, 2012)
PICTURE = (PICTURE_W, PICTURE_H)


def head_top(spot: Spot, top_of_head: float) -> float:
    """Where the top of the head lands on the picture, in its pixels."""
    return spot.top + top_of_head * spot.scale


# ------------------------------------------------------------------ the rule


def test_the_default_placement_is_the_rules_numbers():
    assert (FACE_H, FACE_Y, MAX_SCALE, FACE_MOST, HEAD_TOP) == (0.14, 0.27, 3.0, 0.30, 0.0)
    numbers = Placement(face_h=0.14, face_y=0.27, max_scale=3.0, face_most=0.30, head_top=0.0)
    assert numbers == DEFAULT_PLACEMENT


def test_the_width_never_changes_the_scale():
    """Tall enough to reach the edge, or short enough to grow: the width never
    shrinks anyone, and never stops them growing."""
    face = (100, 20, 120, 120)
    narrow = place((300, 900), face, FRAME)
    wide = place((9000, 900), face, FRAME)
    assert narrow.scale == wide.scale == pytest.approx(0.14 * 2012 / 120)
    short = place((300, 500), face, FRAME)
    short_and_wide = place((9000, 500), face, FRAME)
    assert short.grown and short.scale == short_and_wide.scale > narrow.scale


def test_place_puts_the_face_centre_at_face_y_and_halfway_across():
    spot = place((400, 858), (140, 60, 120, 120), FRAME)
    x, y, width, height = spot.face
    assert spot.scale == pytest.approx(0.14 * 2012 / 120)
    assert y + height / 2 == pytest.approx(0.27 * 2012, abs=0.5)
    assert x + width / 2 == pytest.approx(1412 / 2, abs=0.5)
    assert height == pytest.approx(0.14 * 2012)
    assert (spot.width, spot.height) == (round(400 * spot.scale), round(858 * spot.scale))
    assert not spot.grown


def test_a_cutout_that_ends_above_the_edge_grows_until_it_reaches_it():
    """Cut off at the waist: 500 px of person under the face's centre. At 0.14
    that ends 0.85 of the way down, so the scale grows until the last row is the
    picture's, and the face is still centred at 0.27, bigger."""
    spot = place((500, 600), (190, 40, 120, 120), FRAME)
    assert spot.grown
    assert spot.scale == pytest.approx(0.73 * 2012 / 500)
    assert spot.top + spot.height == FRAME[1]
    _, y, _, height = spot.face
    assert y + height / 2 == pytest.approx(0.27 * 2012, abs=1)
    assert height / 2012 == pytest.approx(0.175, abs=0.001)


def test_growing_stops_at_a_face_0_30_tall_and_the_person_moves_down_onto_the_edge():
    """A head and a neck. With the face 0.30 of the picture tall they still end
    above the edge, so they move down onto it and the face lands below 0.27."""
    spot = place((300, 200), (90, 20, 120, 120), FRAME)
    assert spot.grown
    assert spot.face[3] == pytest.approx(0.30 * 2012)
    assert spot.top + spot.height == FRAME[1]
    _, y, _, height = spot.face
    assert (y + height / 2) / 2012 > 0.27 + 0.01


def test_the_3x_cap_holds_the_face_size_but_not_the_growth():
    """MAX_SCALE stops a small face short of 0.14. Reaching the bottom edge is
    held to a face 0.30 tall and nothing else, so a small photo cut off at the
    chest is enlarged past 3 times to stand on it."""
    tall = place((300, 2000), (130, 30, 40, 40), FRAME)
    assert tall.scale == MAX_SCALE and not tall.grown
    assert tall.face[3] / 2012 == pytest.approx(3 * 40 / 2012)

    short = place((300, 300), (130, 30, 40, 40), FRAME)
    assert short.grown and short.scale > MAX_SCALE
    assert short.top + short.height == FRAME[1]


@pytest.mark.parametrize("frame", [FRAME, PICTURE, (1380, 1510), (997, 1409)])
def test_whole_pixels_never_leave_a_row_of_art_under_the_person(frame):
    """The scale that reaches the edge is exact, and whole pixels are not. Over
    many cutouts the person ends on the picture's last row or past it."""
    rng = np.random.default_rng(37)
    for _ in range(400):
        height = int(rng.integers(120, 3000))
        face_h = int(rng.integers(16, height // 3))
        face_y = int(rng.integers(0, height - face_h))
        spot = place((int(rng.integers(100, 3000)), height), (40, face_y, face_h, face_h), frame)
        assert spot.top + spot.height >= frame[1]


@pytest.mark.parametrize("placement", [DEFAULT_PLACEMENT, Placement(head_top=0.15)])
def test_the_head_never_rises_past_head_top(placement):
    """Any person, any framing: the top of the head lands at or under
    head_top, and the person still stands on the bottom edge. On the card
    head_top is 0, so the top edge never cuts the head. The rule holds for a
    line lower down too."""
    rng = np.random.default_rng(45)
    for _ in range(400):
        height = int(rng.integers(150, 3000))
        face_h = int(rng.integers(16, height // 2))
        face_y = int(rng.integers(0, height - face_h))
        top_of_head = float(rng.integers(0, face_y + 1))
        spot = place((800, height), (300, face_y, face_h, face_h), PICTURE, placement, top_of_head)
        assert head_top(spot, top_of_head) >= placement.head_top * PICTURE[1] - 0.5
        assert spot.top + spot.height >= PICTURE[1]


def test_a_bust_grows_only_as_far_as_the_head_has_room():
    """Cut off at the chest, with the head's top a line 0.15 down: the face
    grows until the person fills the picture from that line to the bottom
    edge, the head on the line and the chest on the edge, short of 0.30."""
    placement = Placement(head_top=0.15)
    spot = place((394, 373), (123, 44, 123, 123), PICTURE, placement, crown=1.0)
    assert spot.grown
    assert head_top(spot, 1.0) == pytest.approx(0.15 * PICTURE[1], abs=1)
    assert spot.top + spot.height >= PICTURE[1]
    assert 0.25 < spot.face[3] / PICTURE[1] < 0.30


# Four real photos, as BiRefNet cut them and a browser's face detector found
# their faces: the crop's size, the face box and the top of the head, all in
# the crop's pixels.
PHOTOS = {
    "cut at the chest": ((394, 373), (123, 44, 123, 123), 1.0),
    "cut at the waist": ((761, 800), (263, 45, 149, 149), 1.0),
    "full swing": ((790, 1094), (594, 10, 86, 86), 0.0),
    "cut above the belt": ((1632, 1801), (500, 185, 429, 429), 1.0),
}

# Where each face lands on FRAME, its centre and its height, and why.
LANDS = {
    # The face stops at 0.30 and the person moves down onto the edge.
    "cut at the chest": (0.348, 0.300),
    # The face grows from 0.14 to 0.16.
    "cut at the waist": (0.270, 0.160),
    # The feet are in the photo: the 3x cap, and the legs run past the edge.
    "full swing": (0.270, 0.128),
    # The face grows to 0.22.
    "cut above the belt": (0.270, 0.223),
}


@pytest.mark.parametrize("photo", sorted(PHOTOS))
def test_where_four_real_photos_land(photo):
    crop, face, _ = PHOTOS[photo]
    spot = place(crop, face, FRAME)
    _, y, _, height = spot.face
    centre, tall = LANDS[photo]
    assert (y + height / 2) / FRAME[1] == pytest.approx(centre, abs=0.001)
    assert height / FRAME[1] == pytest.approx(tall, abs=0.001)


@pytest.mark.parametrize("photo", sorted(PHOTOS))
def test_four_real_photos_stand_on_the_bottom_edge_of_the_card(photo):
    """On the card's picture: the person reaches the bottom edge, and the top
    of the head is on the picture."""
    crop, face, top_of_head = PHOTOS[photo]
    spot = place(crop, face, PICTURE, crown=top_of_head)
    assert spot.top + spot.height >= PICTURE[1]
    assert head_top(spot, top_of_head) >= 0


# The warnings each photo's job gets on the card, from api.qa.
PHOTO_WARNINGS = {
    "cut at the chest": [PLACED_LOW, FACE_LARGE, TIGHT],
    "cut at the waist": [],
    "full swing": [],
    "cut above the belt": [FACE_LARGE, TIGHT],
}


@pytest.mark.parametrize("photo", sorted(PHOTOS))
def test_four_real_photos_carry_the_warnings_for_where_they_land(photo):
    """The face inside the bands, or the warning that says which one it left,
    measured on the card's own picture."""
    crop, face, top_of_head = PHOTOS[photo]
    spot = place(crop, face, PICTURE, crown=top_of_head)
    said = check_spot(spot, PICTURE)
    assert said == PHOTO_WARNINGS[photo]
    _, y, _, height = spot.face
    centre, tall = (y + height / 2) / PICTURE[1], height / PICTURE[1]
    called = [
        word
        for outside, word in (
            (placed_low(centre), PLACED_LOW),
            (placed_high(centre), PLACED_HIGH),
            (tall < SMALL_FACE, FACE_SMALL),
            (tall > LARGE_FACE, FACE_LARGE),
        )
        if outside
    ]
    assert [word for word in said if word != TIGHT] == called


def test_the_tight_photos_are_the_ones_cut_off_above_the_waist():
    """The chest and the belt grow past 0.22 to reach the edge. The waist does not."""
    tight = {
        photo
        for photo, (crop, face, top) in PHOTOS.items()
        if TIGHT in check_spot(place(crop, face, PICTURE, crown=top), PICTURE)
    }
    assert tight == {"cut at the chest", "cut above the belt"}


def test_the_full_swing_is_capped():
    crop, face, _ = PHOTOS["full swing"]
    spot = place(crop, face, FRAME)
    assert spot.scale == 3.0 and not spot.grown
    assert spot.face[3] / FRAME[1] == pytest.approx(3 * 86 / 2012)


# ------------------------------------------------- what the alpha says


def test_the_estimate_is_the_top_15_percent_as_wide_as_the_silhouette_there():
    alpha = Image.new("L", (400, 1000), 0)
    alpha.paste(255, (150, 100, 250, 250))  # the head, rows 100 to 249
    alpha.paste(255, (60, 250, 340, 1100))  # the body, to the bottom
    # 900 rows of person, so the band is the top 135: the head alone.
    assert estimate_face(alpha) == (150.0, 100.0, 100.0, 135.0)
    assert estimate_face(Image.new("L", (50, 50), 0)) is None


def test_the_top_of_the_head_is_the_first_solid_row_over_the_face():
    alpha = Image.new("L", (400, 600), 0)
    alpha.paste(255, (120, 60, 280, 400))  # the head and the body
    alpha.paste(255, (0, 0, 60, 300))  # a bat held up beside them
    assert crown(alpha, (150, 120, 100, 100)) == 60.0
    # Nothing solid over the box: the box's own top.
    assert crown(Image.new("L", (400, 600), 0), (150, 120, 100, 100)) == 120.0


def test_a_body_that_runs_off_the_bottom_of_the_photo_is_cut_there():
    alpha = Image.new("L", (400, 600), 0)
    alpha.paste(255, (100, 50, 300, 600))
    assert cut_lines(alpha) == (("bottom", 100, 300),)


def test_an_elbow_that_curves_back_inside_the_photo_is_not_cut():
    """The elbow's outline turns 7 px short of the right edge. It is whole in
    the photo, and it keeps its outline on the card."""
    alpha = Image.new("L", (400, 600), 0)
    ImageDraw.Draw(alpha).ellipse([193, 100, 393, 300], fill=255)
    assert cut_lines(alpha) == ()


def test_an_arm_that_grazes_the_edge_is_cut_only_where_it_touches():
    """The right arm meets the edge for 33 rows."""
    alpha = Image.new("L", (400, 600), 0)
    alpha.paste(255, (200, 330, 400, 363))
    alpha.paste(255, (150, 100, 300, 600))
    assert cut_lines(alpha) == (("right", 330, 363), ("bottom", 150, 300))


# ------------------------------------------------------------- the face box


def test_the_face_box_moves_with_the_crop_to_the_alpha_box():
    assert shift_face((458, 75, 123, 123), (312, 11, 748, 683)) == (146, 64, 123, 123)
    assert shift_face(None, (312, 11, 748, 683)) is None
    assert shift_face((1, 2, 3, 4), None) == (1, 2, 3, 4)


def test_half_a_face_on_the_cutout_is_enough_and_less_is_not():
    assert face_on((-100, 0, 200, 200), (400, 858))
    assert not face_on((-101, 0, 200, 200), (400, 858))
    assert face_on((100, 758, 200, 200), (400, 858))
    assert not face_on((100, 759, 200, 200), (400, 858))


def test_no_box_or_an_empty_box_is_not_on_the_cutout():
    assert not face_on(None, (400, 858))
    assert not face_on((100, 0, 0, 200), (400, 858))
    assert not face_on((-500, -500, 100, 100), (400, 858))
