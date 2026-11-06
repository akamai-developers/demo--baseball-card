"""Photo QA: what the matte says about how the photo was framed.

Every rule gets a synthetic matte, built to trip exactly one of them, so a
failure names the rule that broke rather than "the fixture changed".
"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from api.qa import (
    FACE_LARGE,
    FACE_SMALL,
    FLOATS,
    FLOATS_AT,
    NO_FACE,
    NO_SUBJECT,
    PLACED_HIGH,
    PLACED_LOW,
    SMALL,
    SMALL_FACE,
    TIGHT,
    TIGHT_FACE,
    check_alpha,
    check_cutout,
    check_face,
    check_spot,
)
from card.compose import PICTURE, find_person
from card.placement import Placement, Spot, place

W, H = 400, 600


def matte(left: int, top: int, right: int, bottom: int, size: tuple[int, int] = (W, H)):
    """An all-transparent frame with one opaque rectangle in it."""
    alpha = np.zeros((size[1], size[0]), dtype=np.uint8)
    alpha[top:bottom, left:right] = 255
    return alpha


def centred(height_fraction: float = 0.7, width_fraction: float = 0.4):
    """A subject that passes every rule, sized by how tall it is."""
    box_h = int(H * height_fraction)
    box_w = int(W * width_fraction)
    left = (W - box_w) // 2
    top = int(H * 0.1)
    return matte(left, top, left + box_w, min(H, top + box_h))


def test_a_well_framed_subject_has_nothing_to_say() -> None:
    assert check_alpha(centred()) == []


def test_a_subject_running_off_the_bottom_is_fine() -> None:
    """A headshot is supposed to run off the bottom. Only three edges are faults."""
    alpha = matte(120, 60, 280, H)
    assert check_alpha(alpha) == []


def test_touching_the_left_edge() -> None:
    assert check_alpha(matte(0, 60, 200, H)) == ["cut off at the left"]


def test_touching_the_right_edge() -> None:
    assert check_alpha(matte(200, 60, W, H)) == ["cut off at the right"]


def test_touching_the_top_edge() -> None:
    assert check_alpha(matte(120, 0, 280, H)) == ["cut off at the top"]


def test_two_edges_at_once_are_both_named() -> None:
    assert check_alpha(matte(0, 0, W, H)) == [
        "cut off at the left",
        "cut off at the right",
        "cut off at the top",
    ]


def test_within_one_percent_of_an_edge_still_counts() -> None:
    """The rule is 1 percent of the frame, not the pixel itself."""
    inside = int(W * 0.01)
    assert check_alpha(matte(inside, 60, 200, H)) == ["cut off at the left"]
    assert check_alpha(matte(inside + 2, 60, 200, H)) == []


def test_a_short_subject_is_small_in_the_frame() -> None:
    # 30 percent of the height, clear of every edge.
    assert check_alpha(centred(height_fraction=0.30)) == [SMALL]


def test_thirty_five_percent_is_the_line() -> None:
    assert check_alpha(centred(height_fraction=0.36)) == []
    assert check_alpha(centred(height_fraction=0.34)) == [SMALL]


def test_almost_nothing_opaque_means_no_subject() -> None:
    """Under 5 percent coverage. Nothing else is reported, because a bounding
    box on a speck measures noise."""
    alpha = matte(10, 10, 40, 40)
    assert check_alpha(alpha) == [NO_SUBJECT]


def test_an_empty_matte_means_no_subject() -> None:
    assert check_alpha(np.zeros((H, W), dtype=np.uint8)) == [NO_SUBJECT]


def test_a_feathered_edge_is_not_the_subject() -> None:
    """Alpha under 128 is the feather the cutout service leaves. It does not
    count towards coverage, and it does not move the bounding box."""
    alpha = centred()
    alpha[0:5, :] = 100
    assert check_alpha(alpha) == []


def test_it_reads_the_alpha_channel_of_a_real_image() -> None:
    image = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    image.paste((200, 150, 120, 255), (120, 60, 280, H))
    assert check_cutout(image) == []


def test_an_image_with_no_alpha_is_all_subject() -> None:
    """An RGB image converts to alpha 255 everywhere, which touches every edge."""
    warnings = check_cutout(Image.new("RGB", (W, H), "white"))
    assert warnings == ["cut off at the left", "cut off at the right", "cut off at the top"]


@pytest.mark.parametrize("size", [(64, 64), (2048, 1536)])
def test_the_rules_are_fractions_not_pixels(size: tuple[int, int]) -> None:
    """The same framing gets the same answer at any resolution."""
    width, height = size
    box_w, box_h = int(width * 0.4), int(height * 0.7)
    left = (width - box_w) // 2
    top = int(height * 0.1)
    alpha = matte(left, top, left + box_w, top + box_h, size)
    assert check_alpha(alpha) == []


# -- the face box ------------------------------------------------------------
#
# A person from (100, 50) to (500, 1000) in a 600 x 1000 photo, 400 x 950 once
# cropped. The face box is in the photo's pixels, as the browser sends it, and
# check_face moves it into the crop the way compose does. The rule makes a face
# 0.14 of the picture's height, 236.9 px of the card's 1692, unless that takes
# more than 3 times: a face under 56.4 px tall ends up under 0.10.

PICTURE_H = PICTURE[1]


def person(size: tuple[int, int] = (600, 1000), body=(100, 50, 500, 1000)) -> Image.Image:
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    image.paste((200, 150, 120, 255), body)
    return image


def test_the_picture_is_the_cards() -> None:
    assert PICTURE == (1412, 1692)


def test_a_face_on_the_person_has_nothing_to_say() -> None:
    assert check_face(person(), (250, 100, 100, 150)) == []


def test_the_browser_finding_no_face_says_so() -> None:
    """The top of the silhouette places the person, and lands where a face goes."""
    assert check_face(person(), None) == [NO_FACE]


def test_a_box_that_is_not_on_the_person_is_no_face() -> None:
    """The box is in the photo's top left corner, and the person starts at (100, 50)."""
    assert check_face(person(), (0, 0, 60, 40)) == [NO_FACE]


def test_a_face_the_cap_leaves_small_says_so() -> None:
    # 50 px tall needs 4.7 times. At 3 it is 150 px on the card, 0.089 of 1692.
    # A full swing across a room looks like this.
    assert check_face(person(), (270, 100, 50, 50)) == [FACE_SMALL]


def test_the_small_line_is_ten_percent_of_the_picture() -> None:
    """57 px at 3 times is 171 px, over 169.2. 56 px is 168, under it."""
    assert SMALL_FACE == 0.10
    assert check_face(person(), (250, 100, 57, 57)) == []
    assert check_face(person(), (250, 100, 56, 56)) == [FACE_SMALL]


def test_a_box_with_nobody_to_measure_against_has_nothing_to_say() -> None:
    """The cutout failed, or came back empty. The box was found, and there is
    nobody on the card for it to be the face of."""
    empty = Image.new("RGBA", (600, 1000), (0, 0, 0, 0))
    assert check_face(empty, (250, 100, 100, 120)) == []
    assert check_face(None, (250, 100, 100, 120)) == []


def test_no_face_is_said_even_with_nobody_on_the_card() -> None:
    """A flat grey photo: the browser finds no face and the matte finds nobody.
    The browser's null is still worth saying, next to "no subject found"."""
    empty = Image.new("RGBA", (600, 1000), (0, 0, 0, 0))
    assert check_face(empty, None) == [NO_FACE]
    assert check_face(None, None) == [NO_FACE]


def test_without_a_face_field_only_the_placement_speaks() -> None:
    """cardctl sends no face field, so nobody looked for a face, and "no face
    found" would not be true. Where the silhouette landed still is: a matte
    that fades out under the feet leaves the person above the edge."""
    assert check_face(person(), None, sent=False) == []
    assert check_face(soft_under_the_feet(), None, sent=False) == [FLOATS]


def test_an_estimated_face_never_trips_a_size_band() -> None:
    """Without a box, the face is the top 15 percent of the silhouette. A
    person that ends above the edge grows until they reach it, and one that
    reaches it already is 0.14 of the picture or the cap's size and up."""
    for height in (110, 300, 700, 950):
        short = person((600, 1000), (100, 1000 - height, 500, 1000))
        assert check_face(short, None, sent=False) == [], height


# -- where the face landed -----------------------------------------------------


def spot_at(centre: float, height: float, size=PICTURE, grown: bool = False) -> Spot:
    """A spot whose face lands with its centre and height at these fractions,
    on a cutout that reaches the picture's bottom edge."""
    face_h = height * size[1]
    top = centre * size[1] - face_h / 2
    return Spot(1.0, 100, size[1], 0, 0, (600.0, top, face_h, face_h), grown)


def soft_under_the_feet() -> Image.Image:
    """A person to the waist, with 250 px of matte under them at alpha 100: a
    shadow BiRefNet was unsure of. The strip is part of the cutout, so compose
    stands it on the edge, and none of it is the person.

    The strip is 40 px wide. compose.clean_edge colours a soft pixel from the
    person a ring at a time, and its cost grows with the area of soft matte
    times its depth: a strip as wide as the person takes 5 s.
    """
    image = person((600, 1000), (100, 50, 500, 450))
    image.paste((200, 150, 120, 100), (280, 450, 320, 700))
    return image


@pytest.mark.parametrize(
    ("centre", "height", "said"),
    [
        (0.27, 0.14, []),
        (0.3401, 0.14, [PLACED_LOW]),
        (0.3399, 0.14, []),
        (0.1999, 0.14, [PLACED_HIGH]),
        (0.2001, 0.14, []),
        (0.27, 0.0999, [FACE_SMALL]),
        (0.27, 0.1001, []),
        (0.27, 0.1901, [FACE_LARGE]),
        (0.27, 0.1899, []),
        (0.45, 0.05, [PLACED_LOW, FACE_SMALL]),
        (0.10, 0.25, [PLACED_HIGH, FACE_LARGE]),
    ],
)
def test_each_band(centre: float, height: float, said: list[str]) -> None:
    assert check_spot(spot_at(centre, height), PICTURE) == said


@pytest.mark.parametrize(
    ("bottom", "said"),
    [(1.0, []), (FLOATS_AT, []), (0.9799, [FLOATS]), (0.5, [FLOATS])],
)
def test_a_person_who_ends_above_0_98_floats(bottom: float, said: list[str]) -> None:
    assert check_spot(spot_at(0.27, 0.14), PICTURE, bottom * PICTURE_H) == said


def test_without_a_measured_bottom_the_cutouts_own_is_used() -> None:
    short = Spot(1.0, 100, 1600, 0, 0, (600.0, 338.0, 237.0, 237.0))
    assert check_spot(short, PICTURE) == [FLOATS]
    assert check_spot(spot_at(0.27, 0.14), PICTURE) == []


@pytest.mark.parametrize(
    ("height", "grown", "said"),
    [
        (0.2201, True, [FACE_LARGE, TIGHT]),
        (0.2199, True, [FACE_LARGE]),
        (0.30, True, [FACE_LARGE, TIGHT]),
        (0.18, True, []),
        # A face this big that did not have to grow came from the rule's own
        # numbers, not from a photo cut off too close.
        (0.25, False, [FACE_LARGE]),
    ],
)
def test_a_face_that_grew_past_0_22_to_reach_the_edge_is_a_tight_photo(
    height: float, grown: bool, said: list[str]
) -> None:
    assert TIGHT_FACE == 0.22
    assert check_spot(spot_at(0.27, height, grown=grown), PICTURE) == said


def test_a_person_cut_off_at_the_chest_is_told_to_step_back() -> None:
    """A selfie's framing: at 0.30 the face still leaves the person short of
    the edge, so they move down onto it and the face lands low."""
    bust = person((400, 400), (40, 200, 360, 400))
    bust.paste((230, 190, 160, 255), (125, 60, 275, 200))
    assert check_face(bust, (125, 60, 150, 150)) == [PLACED_LOW, FACE_LARGE, TIGHT]


def test_floats_is_what_the_card_shows_and_not_the_cutouts_box() -> None:
    """The cutout's box reaches the edge: compose stood the soft strip on it.
    The person, solid, ends 250 px of photo above it, and the warning says so."""
    face = (250, 100, 100, 150)
    cutout = soft_under_the_feet()
    found = find_person(cutout, face)
    spot = place(found.image.size, found.face, PICTURE)
    assert spot.top + spot.height >= PICTURE_H
    assert check_face(cutout, face) == [FACE_LARGE, FLOATS]


def test_the_bands_are_fractions_of_the_picture_asked_about() -> None:
    """The same spot is placed right on a 1324 x 1512 box and low on a shorter one."""
    spot = spot_at(0.27, 0.14, (1324, 1512))
    assert check_spot(spot, (1324, 1512)) == []
    assert check_spot(spot, (1324, 1000)) == [PLACED_LOW, FACE_LARGE]


def test_the_warnings_measure_the_rule_and_not_a_copy_of_its_numbers() -> None:
    """The check goes through the placement compose uses, so a changed rule
    shows up here: a face made 0.22 tall is large. A rule that moves the face
    down to 0.40 is measured from 0.40, so a face there is where it should be,
    and the same face on the rule's own 0.27 is low."""
    face = (250, 100, 100, 150)
    assert check_face(person(), face) == []
    assert check_face(person(), face, placement=Placement(face_h=0.22)) == [FACE_LARGE]
    assert check_face(person(), face, placement=Placement(face_y=0.40)) == []
    lower = spot_at(0.40, 0.14)
    assert check_spot(lower, PICTURE) == [PLACED_LOW]
    assert check_spot(lower, PICTURE, placement=Placement(face_y=0.40)) == []


def test_the_warnings_are_the_words_the_app_shows() -> None:
    assert (PLACED_LOW, PLACED_HIGH, FACE_SMALL, FACE_LARGE, FLOATS, TIGHT) == (
        "placed low",
        "placed high",
        "face small",
        "face large",
        "floats",
        "tight photo, shoot from the waist up or lower",
    )
