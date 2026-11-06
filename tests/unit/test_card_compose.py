"""The Pillow compositor: where the person lands in pixels, and their edges.

The subject is found by colour: the art is black, the cutout's body is red and
its face green, so the green pixels in the result are where the face landed.
That survives the shadow, which only darkens the art.

The edge tests put a cutout over solid magenta. Magenta has no green in it, and
the shadow only darkens, so every bit of green in a pixel came from the person.
A pixel at the edge that is greener than the person just inside it is carrying
something the person does not have: the wall the photo was taken against, or a
light painted round them.
"""

from __future__ import annotations

from unittest import mock

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFilter

import card.compose as compose
from api.qa import (
    FACE_LARGE,
    FACE_SMALL,
    LARGE_FACE,
    PLACED_HIGH,
    PLACED_LOW,
    SMALL_FACE,
    TIGHT,
    check_face,
    placed_high,
    placed_low,
)
from card import PICTURE_H, PICTURE_W
from card.compose import (
    DEFAULT_PLACEMENT,
    EDGE_PX,
    Spot,
    clean_edge,
    compose_hero,
    cover,
    fade_cuts,
    find_person,
    lowest,
    on_picture,
    place_person,
    seal_alpha,
    trim,
)
from card.placement import place, shift_face
from card.render import FIXTURE_CUTOUT, FIXTURE_FACE

SIZE = (400, 560)
# The frame the rule's numbers were first measured on. The card's own picture,
# above the name plate, is PICTURE.
FRAME = (1412, 2012)
PICTURE = (PICTURE_W, PICTURE_H)
MAGENTA = (255, 0, 255)
RED = (255, 0, 0, 255)
GREEN = (0, 255, 0, 255)
BLACK = (0, 0, 0)

# One picture's height in the tolerance.
NEAR = 0.01


def _art(colour=BLACK) -> Image.Image:
    return Image.new("RGB", (300, 420), colour)


def _subject(width: int, height: int) -> Image.Image:
    return Image.new("RGBA", (width, height), (255, 0, 0, 255))


def _fixture() -> Image.Image:
    with Image.open(FIXTURE_CUTOUT) as opened:
        return opened.convert("RGBA")


def figure(size, face, *body) -> Image.Image:
    """A cutout the size of a photo: red rectangles for a body, the face box green on top."""
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    for box in body:
        image.paste(RED, box)
    x, y, width, height = face
    image.paste(GREEN, (x, y, x + width, y + height))
    return image


def _green(hero: Image.Image) -> np.ndarray:
    pixels = np.asarray(hero.convert("RGB"), dtype=np.int16)
    return (pixels[..., 1] > 160) & (pixels[..., 0] < 90) & (pixels[..., 2] < 90)


def landed(hero: Image.Image) -> tuple[float, float]:
    """Where the green face landed: its centre and its height, as fractions of the picture."""
    green = _green(hero)
    rows = np.flatnonzero(green.any(axis=1))
    assert rows.size, "the face is not on the picture"
    top, bottom = rows[0], rows[-1] + 1
    return (top + bottom) / 2 / hero.height, (bottom - top) / hero.height


def _red(hero: Image.Image) -> np.ndarray:
    pixels = np.asarray(hero.convert("RGB"), dtype=np.int16)
    return (pixels[..., 0] > 140) & (pixels[..., 1] < 90) & (pixels[..., 2] < 90)


def _subject_rows(hero: Image.Image) -> np.ndarray:
    return np.flatnonzero(_red(hero).any(axis=1))


def _subject_cols(hero: Image.Image) -> np.ndarray:
    return np.flatnonzero(_red(hero).any(axis=0))


def _bottom_row(hero: Image.Image) -> int:
    """The lowest row with any of the person in it, red body or green face."""
    return int(np.flatnonzero((_red(hero) | _green(hero)).any(axis=1)).max())


def _erode(mask: np.ndarray, radius: int) -> np.ndarray:
    """A real erosion, with a square the size of the radius. The compositor has
    its own, and a test that used it would be marking its own homework."""
    image = Image.fromarray(mask.astype(np.uint8) * 255, "L")
    return np.asarray(image.filter(ImageFilter.MinFilter(2 * radius + 1))) > 0


def _placed(cutout: Image.Image, picture=FRAME, face=None):
    """The person, and where the rule puts them, the way compose_hero finds both."""
    person = find_person(cutout, face)
    return person, place_person(person, picture, DEFAULT_PLACEMENT)


# ------------------------------------------------------------ the picture


@pytest.mark.parametrize("size", [(400, 560), (1412, 2012), (200, 200)])
def test_hero_is_exactly_the_size_that_was_asked_for(size):
    assert compose_hero(_art(), _subject(120, 240), size).size == size


def test_the_hero_is_the_cards_picture_when_no_size_is_given():
    assert compose_hero(_art(), _subject(120, 240)).size == PICTURE == (1412, 1692)


def test_cover_fills_the_frame_without_squashing_the_picture():
    wide = Image.new("RGB", (1000, 200), (10, 20, 30))
    assert cover(wide, SIZE).size == SIZE


def test_no_cutout_gives_back_the_art_alone():
    hero = compose_hero(_art(), None, SIZE)
    assert hero.size == SIZE
    assert _subject_rows(hero).size == 0


def test_a_fully_transparent_cutout_is_treated_as_no_cutout():
    empty = Image.new("RGBA", (200, 200), (255, 0, 0, 0))
    assert _subject_rows(compose_hero(_art(), empty, SIZE)).size == 0
    assert find_person(empty) is None


def test_the_same_inputs_give_the_same_pixels():
    first = compose_hero(_art(), _fixture(), SIZE, face=FIXTURE_FACE)
    second = compose_hero(_art(), _fixture(), SIZE, face=FIXTURE_FACE)
    assert first.tobytes() == second.tobytes()


def test_the_checked_in_fixture_cutout_still_has_a_subject_in_it():
    with Image.open(FIXTURE_CUTOUT) as cutout:
        box = cutout.convert("RGBA").getchannel("A").getbbox()
    assert box is not None
    left, top, right, bottom = box
    # The wall is gone on both sides, and the subject runs off the bottom edge.
    assert left > 40
    assert right < cutout.width - 40
    assert bottom == cutout.height


def test_on_picture_resizes_only_what_lands_and_says_where():
    image = Image.new("RGBA", (100, 200), RED)
    spot = place((100, 200), (40, 10, 20, 20), (300, 300))
    shown, (left, top) = on_picture(image, spot, (300, 300))
    assert (left, top) == (max(spot.left, 0), max(spot.top, 0))
    assert shown.size == (
        min(spot.left + spot.width, 300) - left,
        min(spot.top + spot.height, 300) - top,
    )


# ------------------------------------------------------- where people land

# Six people as photos: red bodies and a green face box on nothing. Each is the
# photo's size, the face box, the body, and whether the browser sent the box.
PEOPLE = {
    # Feet in the photo. At 0.14 the legs run far past the bottom edge.
    "full body": ((600, 1800), (250, 100, 100, 120), [(150, 220, 450, 1750)], True),
    # A bat across the photo. It leaves the picture at both sides.
    "bat": ((1600, 1200), (750, 100, 100, 120), [(650, 220, 950, 1200), (0, 500, 1600, 540)], True),
    # Cut off at the waist, 3.5 faces under the face's centre: the face grows
    # from 0.14 to 0.21 for the person to reach the edge.
    "waist up": ((800, 900), (300, 100, 200, 200), [(150, 300, 650, 900)], True),
    # Cut off at the chest. At a face 0.30 tall the person still ends above
    # the edge, so they move down onto it, the face with them.
    "head and shoulders": ((400, 400), (125, 60, 150, 150), [(40, 200, 360, 400)], True),
    # Elbows out and cut off at the chest. The face grows to 0.20, the width
    # with it, and the sides crop.
    "wide": ((1600, 600), (750, 100, 100, 120), [(100, 220, 1500, 600)], True),
    # cardctl sends no box. The top 15 percent of the silhouette is the face.
    "no face box": ((400, 1000), (150, 0, 100, 150), [(100, 150, 300, 1000)], False),
}


@pytest.mark.parametrize("picture", [FRAME, PICTURE], ids=["frame", "card"])
@pytest.mark.parametrize("name", list(PEOPLE))
def test_every_person_stands_on_the_bottom_edge(name, picture):
    """For each one, the bottom on the edge and the top of the head on the
    picture, measured in the pixels compose drew."""
    size, head, body, sent = PEOPLE[name]
    cutout = figure(size, head, *body)
    hero = compose_hero(_art(), cutout, picture, face=head if sent else None)
    assert _bottom_row(hero) == picture[1] - 1, "the person reaches the last row"
    assert not _green(hero)[0].any(), "the top edge cuts the face"


# The warnings each person's job gets on the card, from api.qa. Every one
# reaches the bottom edge, so the only words are about the face.
WARNED = {
    "full body": [],
    "bat": [],
    "waist up": [FACE_LARGE],
    "head and shoulders": [PLACED_LOW, FACE_LARGE, TIGHT],
    "wide": [FACE_LARGE],
    "no face box": [],
}


@pytest.mark.parametrize("name", list(PEOPLE))
def test_every_person_carries_the_warnings_for_where_their_face_landed(name):
    """The face measured in the pixels compose drew is inside the bands, or
    the job carries the warning that names the band. The check uses compose's
    arithmetic without drawing, so this holds the two together."""
    size, head, body, sent = PEOPLE[name]
    cutout = figure(size, head, *body)
    face = head if sent else None
    hero = compose_hero(_art(), cutout, PICTURE, face=face)

    said = check_face(cutout, face, sent=sent)
    assert said == WARNED[name]
    centre, height = landed(hero)
    called = [
        word
        for outside, word in (
            (placed_low(centre), PLACED_LOW),
            (placed_high(centre), PLACED_HIGH),
            (height < SMALL_FACE, FACE_SMALL),
            (height > LARGE_FACE, FACE_LARGE),
        )
        if outside
    ]
    assert [word for word in said if word != TIGHT] == called


def test_a_tall_cutout_lands_its_face_at_the_target_and_runs_off_the_bottom():
    """A whole person: a face 120 px tall and a body running off the photo."""
    tall = figure((500, 1500), (200, 60, 100, 120), (100, 180, 400, 1500))
    hero = compose_hero(_art(), tall, FRAME, face=(200, 60, 100, 120))

    centre, height = landed(hero)
    assert centre == pytest.approx(0.27, abs=NEAR)
    assert height == pytest.approx(0.14, abs=NEAR)
    # The legs leave the picture at the bottom, the way a card crops a photo.
    assert _subject_rows(hero).max() == FRAME[1] - 1


def test_a_wide_cutout_with_a_bat_keeps_its_face_size_and_loses_its_sides():
    """The face keeps 0.14, and the bat leaves the picture on both sides."""
    face = (750, 60, 100, 120)
    batter = figure((1600, 1000), face, (650, 180, 950, 1000), (0, 450, 1600, 490))
    hero = compose_hero(_art(), batter, FRAME, face=face)

    centre, height = landed(hero)
    assert centre == pytest.approx(0.27, abs=NEAR)
    assert height == pytest.approx(0.14, abs=NEAR)
    cols = _subject_cols(hero)
    assert cols.min() == 0 and cols.max() == FRAME[0] - 1


def test_a_small_face_stops_at_3x_and_stays_centred():
    """A 40 px face on a tall body would need 7 times. It gets 3, so it is
    smaller than the target, and its centre is still at 0.27."""
    small = figure((300, 1000), (130, 30, 40, 40), (80, 70, 220, 1000))
    hero = compose_hero(_art(), small, FRAME, face=(130, 30, 40, 40))

    centre, height = landed(hero)
    assert centre == pytest.approx(0.27, abs=NEAR)
    assert height == pytest.approx(3 * 40 / 2012, abs=NEAR)


def test_no_face_box_puts_the_top_of_the_silhouette_where_a_face_goes():
    """cardctl sends no box. The top 15 percent of the silhouette is the head:
    here a green block 150 px tall on a body 850 px tall."""
    body = figure((400, 1000), (150, 0, 100, 150), (100, 150, 300, 1000))
    hero = compose_hero(_art(), body, FRAME)

    centre, height = landed(hero)
    assert centre == pytest.approx(0.27, abs=NEAR)
    assert height == pytest.approx(0.14, abs=NEAR)


def test_a_face_box_off_the_cutout_is_no_box():
    body = figure((400, 1000), (150, 0, 100, 150), (100, 150, 300, 1000))
    off = compose_hero(_art(), body, FRAME, face=(-500, -500, 100, 100))
    assert off.tobytes() == compose_hero(_art(), body, FRAME).tobytes()
    assert find_person(body, (-500, -500, 100, 100)).estimated


def test_the_browsers_box_is_moved_into_the_crop_and_kept():
    person = find_person(_fixture(), FIXTURE_FACE)
    assert not person.estimated
    assert person.face == shift_face(FIXTURE_FACE, person.box)


def test_the_fixture_stands_on_the_plate_with_its_head_on_the_card():
    """The test photo, cut off at the hips, on the card's own picture: it grows
    to reach the bottom edge, which is the plate's top rule, and the face stays
    centred 0.27 of the way down."""
    person, spot = _placed(_fixture(), PICTURE, FIXTURE_FACE)
    assert spot.grown
    assert spot.top + spot.height >= PICTURE[1]
    assert lowest(person, spot, PICTURE) == PICTURE[1]
    _, y, _, height = spot.face
    assert (y + height / 2) / PICTURE[1] == pytest.approx(0.27, abs=0.002)
    assert spot.top + person.crown * spot.scale > 0


# ----------------------------------------------------------------- the cuts


def test_a_cut_at_the_bottom_of_the_photo_lands_on_the_edge_and_keeps_every_pixel():
    """A bust cut off at the chest stands on that cut. Nothing fades toward it,
    and the picture's last row is the person."""
    face = (125, 60, 150, 150)
    headshot = figure((400, 400), face, (40, 200, 360, 400))
    person, spot = _placed(headshot, face=face)
    assert person.cut == (("bottom", 40, 360),)
    assert fade_cuts(person, spot, FRAME) is person.image

    hero = compose_hero(_art(), headshot, FRAME, face=face)
    red = np.asarray(hero, dtype=np.int16)[..., 0]
    middle = FRAME[0] // 2
    assert (red[-1, middle - 100 : middle + 100] > 240).all()


def _hand_and_body(size, strip) -> Image.Image:
    """A body from (100, 50) to (250, 300), and a strip of hand lower down."""
    return figure(size, (150, 60, 50, 50), (100, 50, 250, 300), strip)


def test_the_lowest_row_is_counted_after_the_fade():
    """A hand the photo cut off at its left edge, below the body. On a picture
    where that edge shows, the hand fades out, and the person the card shows
    ends with the body, 20 rows higher."""
    cutout = _hand_and_body((400, 400), (0, 300, 60, 320))
    person = find_person(cutout, (150, 60, 50, 50))
    assert person.cut == (("left", 300, 320),)
    width, height = person.image.size
    spot = Spot(1.0, width, height, 100, 0, (250.0, 10.0, 50.0, 50.0))
    assert lowest(person, spot, FRAME) == 250


def test_the_lowest_row_is_counted_only_where_it_lands_on_the_picture():
    """The same hand, whole this time, cropped off the picture's left side."""
    cutout = _hand_and_body((500, 400), (20, 300, 80, 320))
    person = find_person(cutout, (150, 60, 50, 50))
    width, height = person.image.size
    on = Spot(1.0, width, height, 100, 0, (230.0, 10.0, 50.0, 50.0))
    off = Spot(1.0, width, height, -70, 0, (60.0, 10.0, 50.0, 50.0))
    assert lowest(person, on, FRAME) == 270
    assert lowest(person, off, FRAME) == 250
    assert lowest(person, Spot(1.0, width, height, 0, 1900, (0, 0, 1, 1)), FRAME) == FRAME[1]


def test_a_cut_below_the_picture_keeps_every_pixel():
    """The tall cutout's legs run off the photo and off the card. Nobody sees
    that cut, so nothing fades."""
    face = (200, 60, 100, 120)
    person = find_person(figure((500, 1500), face, (100, 180, 400, 1500)), face)
    spot = place(person.image.size, person.face, FRAME)
    assert spot.top + spot.height > FRAME[1]
    assert fade_cuts(person, spot, FRAME) is person.image


def test_the_fade_never_reaches_the_face():
    """A photo cut off just under the chin: the fade would reach up into the face,
    so it stops a quarter of a face short of it instead."""
    face = (100, 100, 200, 200)
    person = find_person(figure((400, 330), face, (40, 300, 360, 330)), face)
    spot = place(person.image.size, person.face, FRAME)
    faded = fade_cuts(person, spot, FRAME)
    x, y, width, height = (round(v) for v in person.face)
    alpha = np.asarray(faded.getchannel("A"))[y : y + height + height // 4, x : x + width]
    assert (alpha == 255).all()
    rgb = np.asarray(faded.convert("RGB"))[y : y + height, x : x + width]
    assert (rgb == np.asarray(person.image.convert("RGB"))[y : y + height, x : x + width]).all()


def test_a_face_at_the_edge_of_the_photo_leaves_that_cut_a_cut():
    """A chin on the photo's bottom edge leaves no room for a fade at all."""
    face = (100, 100, 200, 200)
    person = find_person(figure((400, 300), face, (40, 280, 360, 300)), face)
    spot = place(person.image.size, person.face, FRAME)
    assert fade_cuts(person, spot, FRAME) is person.image


def test_a_graze_fades_only_near_where_it_touches():
    face = (100, 40, 100, 100)
    cutout = figure((400, 600), face, (200, 330, 400, 363), (60, 140, 300, 600))
    person = find_person(cutout, face)
    # A picture so wide that the photo's right edge lands on it.
    spot = place(person.image.size, person.face, (8000, 6000))
    faded = np.asarray(fade_cuts(person, spot, (8000, 6000)).getchannel("A"))
    x0, y0 = person.box[0], person.box[1]
    assert faded[346 - y0, 399 - x0] < 60, "gone where the arm met the edge"
    assert faded[200 - y0, 299 - x0] == 255, "the body far above the graze is whole"


# ---------------------------------------------------------------- the shadow


def test_the_shadow_is_at_the_feet_and_not_around_the_head():
    grey = Image.new("RGB", SIZE, (128, 128, 128))
    hero = compose_hero(grey, _subject(120, 240), SIZE)
    pixels = np.asarray(hero, dtype=np.int16)
    beside = slice(_subject_cols(hero).max() + 2, _subject_cols(hero).max() + 12)

    at_the_head = pixels[int(SIZE[1] * 0.22) : int(SIZE[1] * 0.30), beside].mean()
    at_the_feet = pixels[SIZE[1] - 40 : SIZE[1] - 2, beside].mean()

    assert at_the_feet < at_the_head - 20
    assert at_the_head > 120, "the art beside the head should be left alone"


def test_the_shadow_is_never_drawn_on_top_of_the_subject():
    grey = Image.new("RGB", (300, 420), (128, 128, 128))
    subject = Image.new("RGBA", (400, 900), (200, 200, 200, 255))
    hero = compose_hero(grey, subject, FRAME)
    _, spot = _placed(subject)

    # The bottom of the subject is where the ground pool is darkest.
    bottom = min(spot.top + spot.height, FRAME[1])
    feet = np.asarray(hero, dtype=np.int16)[
        bottom - 30 : bottom - 2,
        max(spot.left, 0) + 40 : min(spot.left + spot.width, FRAME[0]) - 40,
    ]
    assert feet.min() >= 199


def _pillow_shadow(alpha: Image.Image, size, left: int, top: int) -> np.ndarray:
    """The shadow as the compositor drew it with Pillow, over the whole picture."""
    frame_w, frame_h = size
    width, height = alpha.size
    drop = round(width * 0.025)
    halo = Image.new("L", size, 0)
    halo.paste(alpha, (left + drop, top + drop))
    halo = halo.filter(ImageFilter.GaussianBlur(width * 0.045))
    rows = np.arange(frame_h, dtype=np.float32)[:, None]
    fade_from = top + height * 0.35
    fade_to = top + height * 0.90
    ramp = np.clip((rows - fade_from) / max(fade_to - fade_from, 1.0), 0.0, 1.0)
    faded = np.asarray(halo, dtype=np.float32) * ramp
    ground = Image.new("L", size, 0)
    if top + height >= frame_h:
        centre, across, up = left + width // 2, width * 0.46, width * 0.085
        ImageDraw.Draw(ground).ellipse(
            [centre - across, frame_h - up, centre + across, frame_h + up], fill=255
        )
        ground = ground.filter(ImageFilter.GaussianBlur(width * 0.045))
    covered = Image.new("L", size, 0)
    covered.paste(alpha, (left, top))
    in_front = np.asarray(covered, dtype=np.float32) / 255.0
    pool = np.maximum(faded, np.asarray(ground, dtype=np.float32)) * (1.0 - in_front)
    return pool.astype(np.uint8)


def _oval(width: int, height: int) -> Image.Image:
    alpha = Image.new("L", (width, height), 0)
    ImageDraw.Draw(alpha).ellipse([0, 0, width - 1, height * 2], fill=255)
    return alpha


@pytest.mark.parametrize(
    ("alpha", "left", "top", "levels"),
    [
        # Head at the top, runs to the floor.
        (_oval(900, 1612), 256, 400, 1),
        # Short and wide on the floor: the pool reaches higher than the halo does.
        (_oval(1300, 160), 56, 1852, 1),
        # Stops short of the floor, so no pool.
        (_oval(600, 900), 400, 300, 1),
        # Narrower than any person the face rule places, and a blur 9 px wide.
        (_oval(200, 400), 600, 1612, 3),
    ],
    ids=["tall", "short and wide", "off the floor", "narrow"],
)
def test_the_shadow_is_pillows_shadow_blurred_in_opencv(alpha, left, top, levels):
    """OpenCV blurs the shadow in three box passes, as Pillow does, in a tenth
    of the time. Every row is within a level of Pillow's for a person 300 px
    wide or more, and above the row the shadow starts at there is none in
    Pillow's either."""
    shadow, first = compose._shadow_mask(alpha, FRAME, left, top)
    ours = np.zeros((FRAME[1], FRAME[0]), dtype=np.int16)
    ours[first:] = shadow
    theirs = _pillow_shadow(alpha, FRAME, left, top).astype(np.int16)
    assert theirs.max() > 40, "the case has a shadow to compare"
    assert np.abs(ours - theirs).max() <= levels
    assert theirs[:first].max() <= levels


# ------------------------------------------------------ nothing touches the face


def test_the_photo_survives_compositing_over_a_screaming_background():
    """Magenta under the fixture, and no magenta in the face.

    A hole in the matte, a shadow drawn over the subject or an edge clean-up
    that wandered inland would all show up here as a pixel that is no longer
    the one the camera recorded.
    """
    cutout = _fixture()
    hero = compose_hero(Image.new("RGB", (300, 420), MAGENTA), cutout, FRAME)

    person, spot = _placed(cutout)
    # The photo as it came in, cropped and scaled the same way, and not cleaned.
    raw = cutout.crop(person.box).resize((spot.width, spot.height), Image.LANCZOS)
    top, left = max(spot.top, 0), max(spot.left, 0)
    bottom = min(spot.top + spot.height, FRAME[1])
    right = min(spot.left + spot.width, FRAME[0])
    got = np.asarray(hero, dtype=np.int16)[top:bottom, left:right]
    want = np.asarray(raw.convert("RGB"), dtype=np.int16)[
        top - spot.top : bottom - spot.top, left - spot.left : right - spot.left
    ]

    # Well inside the silhouette: clear of the edge band and of the resampling.
    alpha = np.asarray(raw.getchannel("A"))[
        top - spot.top : bottom - spot.top, left - spot.left : right - spot.left
    ]
    core = _erode(alpha > 250, 12)
    assert core.sum() > 100_000, "the eroded subject should still be most of a person"
    assert np.abs(got - want).max(axis=2)[core].max() <= 1


def test_a_hole_in_the_matte_is_filled_before_anything_is_drawn():
    holed = Image.new("RGBA", (400, 900), (120, 60, 30, 255))
    holed.paste((0, 0, 0, 0), (150, 300, 250, 400))
    assert holed.getchannel("A").getpixel((200, 350)) == 0

    hero = compose_hero(Image.new("RGB", (300, 420), MAGENTA), holed, FRAME)
    person, spot = _placed(holed)
    assert person.image.getchannel("A").getpixel((200, 350)) == 255

    magenta = (np.asarray(hero) == np.array(MAGENTA, dtype=np.uint8)).all(axis=2)
    top, left = max(spot.top, 0) + 40, max(spot.left, 0) + 40
    bottom = min(spot.top + spot.height, FRAME[1]) - 40
    right = min(spot.left + spot.width, FRAME[0]) - 40
    assert not magenta[top:bottom, left:right].any()


def test_a_matte_that_is_only_half_sure_about_the_face_is_made_solid():
    """BiRefNet hedging over dark hair is the same defect as a hole, so it goes too."""
    weak = Image.new("L", (200, 200), 0)
    weak.paste(255, (40, 40, 160, 160))
    weak.paste(140, (70, 70, 130, 130))

    sealed = seal_alpha(weak)
    assert sealed.getpixel((100, 100)) == 255
    # The outline does not move.
    assert sealed.getpixel((20, 20)) == 0
    assert sealed.getpixel((39, 100)) == 0


def test_a_face_the_matte_calls_background_is_filled_when_the_person_encloses_it():
    """Alpha 40 in the middle of a solid head is a hole, and a hole is filled."""
    head = Image.new("L", (200, 200), 0)
    head.paste(255, (40, 40, 160, 160))
    head.paste(40, (80, 80, 120, 120))
    assert seal_alpha(head).getpixel((100, 100)) == 255


def test_a_soft_notch_open_to_the_background_stays_soft():
    """Where an ear meets the head, BiRefNet hedges at 5 to 60 percent over a
    patch of wall. A seal that counted anything above 3 percent as the person
    made that patch solid, which put a fleck of wall beside each ear. Under
    the 50 percent line and open to the outside, it is background."""
    head = Image.new("L", (200, 200), 0)
    head.paste(255, (40, 40, 160, 160))
    # A notch 20 px wide cut in from the top edge, at alpha 40.
    head.paste(40, (90, 20, 110, 100))

    sealed = seal_alpha(head)
    assert sealed.getpixel((100, 70)) == 40


def test_sealing_a_matte_with_nothing_wrong_with_it_changes_nothing():
    clean = Image.new("L", (200, 200), 0)
    clean.paste(255, (40, 40, 160, 160))
    assert seal_alpha(clean).tobytes() == clean.tobytes()


def test_the_edge_clean_up_leaves_the_core_exactly_as_the_camera_took_it():
    cutout = _fixture()
    cleaned = clean_edge(cutout)
    alpha = np.asarray(cutout.getchannel("A"))
    core = _erode(alpha >= 128, EDGE_PX + 1)
    assert core.sum() > 100_000

    before = np.asarray(cutout.convert("RGB"))[core]
    after = np.asarray(cleaned.convert("RGB"))[core]
    assert (before == after).all()
    assert (np.asarray(cleaned.getchannel("A"))[core] == 255).all()


# ------------------------------------------------------------------- the edge


def _at_scale_1(cutout_size, face, size, placement=DEFAULT_PLACEMENT, crown=None) -> Spot:
    """A spot that leaves the cutout at its own size, in the middle of the picture."""
    width, height = cutout_size
    left, top = (size[0] - width) // 2, (size[1] - height) // 2
    x, y, face_w, face_h = face
    return Spot(1.0, width, height, left, top, (left + x, top + y, face_w, face_h))


def _whole(cutout: Image.Image, picture, art=MAGENTA):
    """Compose at scale 1 with no fade, so the edge is measured on its own."""
    with mock.patch.object(compose, "FADE", 0.0), mock.patch.object(compose, "place", _at_scale_1):
        hero = compose_hero(Image.new("RGB", (300, 420), art), cutout, picture)
    person = find_person(cutout)
    spot = _at_scale_1(person.image.size, person.face, picture)
    assert spot.left >= 0 and spot.top >= 0, "the whole person is on the picture"
    assert spot.left + spot.width <= picture[0] and spot.top + spot.height <= picture[1]
    return hero, person, spot


def _edge_excess(cutout: Image.Image, tolerance: int = 0) -> np.ndarray:
    """Over magenta, at scale 1: how much greener each edge pixel is than the
    greenest of the person within a few pixels inside it, less `tolerance`.

    The edge is the outer EDGE_PX pixels of the matte, measured from its 50
    percent line, and everything outside that line with any alpha at all.
    """
    hero, person, spot = _whole(cutout, (1600, 2400))
    green = np.asarray(hero, dtype=np.int16)[
        spot.top : spot.top + spot.height, spot.left : spot.left + spot.width, 1
    ]

    alpha = np.asarray(cutout.crop(person.box).getchannel("A"))
    core = _erode(alpha >= 128, EDGE_PX)
    edge = (alpha > 0) & ~core
    reach = 2 * (EDGE_PX + 3) + 1
    core_green = Image.fromarray(np.where(core, green, 0).astype(np.uint8), "L")
    inside = np.asarray(core_green.filter(ImageFilter.MaxFilter(reach)), dtype=np.int16)
    near_core = np.asarray(
        Image.fromarray(core.astype(np.uint8) * 255, "L").filter(ImageFilter.MaxFilter(reach))
    )
    measured = edge & (near_core > 0)
    assert measured.sum() > 1000, "the edge should have been measured somewhere"
    return (green - inside - tolerance)[measured]


def test_no_pixel_at_the_edge_is_lighter_than_the_person_inside_it():
    """The test for a glow, on the fixture over solid magenta.

    A rim light paints a light band down one side, and the matte's outer
    pixels carry the tan wall. Either one fails this.
    """
    excess = _edge_excess(_fixture(), tolerance=8)
    assert excess.max() <= 0, f"{(excess > 0).sum()} edge pixels are lighter than the person"


def _dark_person_on_a_white_wall() -> Image.Image:
    """A dark disc photographed against a white wall, with a matte like BiRefNet's.

    The photo blends person and wall across a pixel or so of edge, and the
    matte's feather reaches several pixels past the person, onto pixels that are
    all wall.
    """
    size = 240
    person = Image.new("L", (size, size), 0)
    ImageDraw.Draw(person).ellipse([40, 40, 200, 200], fill=255)
    coverage = np.asarray(person.filter(ImageFilter.GaussianBlur(0.8)), dtype=np.float32) / 255
    photo = coverage[..., None] * np.float32(40) + (1 - coverage[..., None]) * np.float32(250)
    matte = np.asarray(person.filter(ImageFilter.GaussianBlur(3.0)))

    cutout = Image.fromarray(np.round(photo).astype(np.uint8).repeat(3, axis=2), "RGB")
    cutout.putalpha(Image.fromarray(matte, "L"))
    return cutout


def test_a_white_wall_leaves_no_light_line_round_a_dark_person():
    """A dark head against a white wall, built by hand."""
    excess = _edge_excess(_dark_person_on_a_white_wall(), tolerance=8)
    assert excess.max() <= 0


def test_the_edge_check_catches_the_line_the_raw_matte_leaves():
    """The same person pasted with the photo's own edge pixels fails the check
    above. So the check can see a glow."""
    cutout = _dark_person_on_a_white_wall()
    pasted = Image.new("RGB", cutout.size, MAGENTA)
    pasted.paste(cutout, (0, 0), cutout)
    green = np.asarray(pasted, dtype=np.int16)[..., 1]

    alpha = np.asarray(cutout.getchannel("A"))
    edge = (alpha > 0) & ~_erode(alpha >= 128, EDGE_PX)
    assert green[edge].max() > 40 + 30, "the raw edge carries the white wall"


def test_the_soft_edge_is_twice_as_steep_about_the_50_percent_line():
    """A ramp from 0 to 255 across 256 px, beside a solid block for a core."""
    ramp = np.tile(np.arange(256, dtype=np.uint8), (64, 1))
    alpha = np.concatenate([ramp, np.full((64, 64), 255, dtype=np.uint8)], axis=1)
    cutout = Image.new("RGBA", (alpha.shape[1], 64), (90, 60, 40, 255))
    cutout.putalpha(Image.fromarray(alpha, "L"))

    cleaned = np.asarray(clean_edge(cutout).getchannel("A"))[32].astype(int)
    assert cleaned[:60].max() == 0, "under a quarter is gone"
    assert abs(cleaned[128] - 128) <= 1, "the 50 percent line does not move"
    assert abs(cleaned[96] - 64) <= 1, "three eighths is a quarter"
    assert cleaned[196:].min() == 255, "over three quarters is solid"


def test_the_edge_takes_the_colour_of_the_person_just_inside_it():
    cleaned = clean_edge(_dark_person_on_a_white_wall())
    rgb = np.asarray(cleaned.convert("RGB"), dtype=np.int16)
    alpha = np.asarray(cleaned.getchannel("A"))
    # Every pixel anyone can see is the person's dark grey, the edge included.
    assert rgb[alpha > 0].max() <= 40 + 8


def test_clean_edge_leaves_a_cutout_with_nobody_in_it_alone():
    empty = Image.new("RGBA", (50, 50), (10, 20, 30, 0))
    assert clean_edge(empty).tobytes() == empty.tobytes()
    assert trim(empty) is None


def test_the_edge_clean_up_gives_the_same_bytes_twice():
    assert clean_edge(_fixture()).tobytes() == clean_edge(_fixture()).tobytes()


def test_a_person_who_runs_off_the_bottom_of_the_card_reaches_the_bottom_row():
    """The photo's own border is not an edge, so the clean-up leaves the bottom
    row alone. Placed without a face box, the fixture runs past the bottom of
    the picture, and the person is there in its last row."""
    cutout = _fixture()
    image, box = trim(cutout)
    assert box[3] == cutout.height
    middle = image.width // 2
    assert image.getpixel((middle, image.height - 1)) == cutout.getpixel(
        (box[0] + middle, cutout.height - 1)
    )

    hero = compose_hero(Image.new("RGB", (300, 420), MAGENTA), cutout, FRAME)
    _, spot = _placed(cutout)
    assert spot.top + spot.height > FRAME[1]
    # Magenta and the shadow on it have no green. The person's jeans do.
    centre = spot.left + spot.width // 2
    assert (np.asarray(hero)[-1, centre - 40 : centre + 40, 1] > 0).all()
