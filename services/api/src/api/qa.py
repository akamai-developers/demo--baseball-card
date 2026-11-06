"""What the cutout says about the photo.

The matte is the only honest read on how a photo was framed. Once BiRefNet (or
the mock colour key) has decided which pixels are the person, the bounding box of
that decision tells you whether they were cut off by the edge of the frame,
whether they stood too far back, and whether anyone was found at all.

Three rules, from the alpha channel and nothing else:

    touching an edge     the box starts within 1 percent of the left, right or
                         top edge. The bottom is not a fault: a headshot is
                         supposed to run off the bottom of the frame
    small in the frame   the box is shorter than 35 percent of the photo
    no subject           less than 5 percent of the photo is opaque

None of them fails a card. They come back as strings on the job, the app prints
them next to the card, and a person decides whether to shoot it again.

One more comes from the face box, when the post had a face field:

    no face found   the browser found no face, or the box it found is not on
                    the cutout. The top of the silhouette places the person
                    instead

And six say where the person landed on the card, as fractions of the
picture's height, measured with compose's own arithmetic on the face that
placed them. The rule in card/placement.py stands the person on the bottom
edge, puts the face's centre 0.27 of the way down and makes it 0.14 tall,
unless it had to grow to reach the edge. So these speak when something moved
it: a photo cut off above the hips, the 3x cap on a small face, a head that
would have left the top of the picture, or a rule that changed since.

    placed low      the face's centre is more than 0.07 below face_y
    placed high     the face's centre is more than 0.07 above face_y
    face small      the face is under 0.10 tall
    face large      the face is over 0.19 tall
    floats          the person's lowest solid row ends above 0.98
    tight photo     the face grew past 0.22 tall to put the person on the
                    bottom edge. The warning says what to do about it
"""

from __future__ import annotations

import numpy as np
from PIL import Image

from card.compose import PICTURE, find_person, lowest, place_person
from card.placement import DEFAULT_PLACEMENT, Box, Placement, Spot

# Alpha at or above this is the subject. Below it is the feathered edge or the
# background. The same threshold the compositor treats as solid.
OPAQUE = 128

# A box that starts this close to an edge was cut by it.
EDGE_FRACTION = 0.01
# Shorter than this fraction of the photo and the head is too small to carry a card.
MIN_HEIGHT_FRACTION = 0.35
# Less opaque than this and there is no one in the picture.
MIN_COVERAGE = 0.05

# Where the face may land, as fractions of the picture's height. The face's
# centre within PLACED_BY of face_y, its height between SMALL_FACE and
# LARGE_FACE. A face under 0.10 is too small to read from across a room.
PLACED_BY = 0.07
SMALL_FACE = 0.10
LARGE_FACE = 0.19
# The person stands on the bottom edge when their lowest solid row reaches
# FLOATS_AT of the way down. A face that had to grow past TIGHT_FACE to get them
# there came from a photo cut off above the waist, with less than 3.3 face
# heights of person under the face's centre.
FLOATS_AT = 0.98
TIGHT_FACE = 0.22

NO_SUBJECT = "no subject found"
SMALL = "subject is small in the frame"
CUT_OFF = "cut off at the {}"
NO_FACE = "no face found"
PLACED_LOW = "placed low"
PLACED_HIGH = "placed high"
FACE_SMALL = "face small"
FACE_LARGE = "face large"
FLOATS = "floats"
TIGHT = "tight photo, shoot from the waist up or lower"


def alpha_array(image: Image.Image) -> np.ndarray:
    """The alpha channel as a 2D uint8 array. A photo with no alpha is all solid."""
    if image.mode != "RGBA":
        image = image.convert("RGBA")
    return np.asarray(image.getchannel("A"), dtype=np.uint8)


def check_alpha(alpha: np.ndarray) -> list[str]:
    """The warnings for one matte, in a fixed order so two runs read the same."""
    if alpha.ndim != 2 or alpha.size == 0:
        return [NO_SUBJECT]

    height, width = alpha.shape
    solid = alpha >= OPAQUE
    coverage = float(solid.mean())
    if coverage < MIN_COVERAGE:
        # Any box measured on this would be measuring noise, so it stops here.
        return [NO_SUBJECT]

    rows = np.flatnonzero(solid.any(axis=1))
    columns = np.flatnonzero(solid.any(axis=0))
    top, bottom = int(rows[0]), int(rows[-1])
    left, right = int(columns[0]), int(columns[-1])

    margin_x = EDGE_FRACTION * width
    margin_y = EDGE_FRACTION * height

    warnings = []
    if left <= margin_x:
        warnings.append(CUT_OFF.format("left"))
    if right >= width - 1 - margin_x:
        warnings.append(CUT_OFF.format("right"))
    if top <= margin_y:
        warnings.append(CUT_OFF.format("top"))
    if (bottom - top + 1) < MIN_HEIGHT_FRACTION * height:
        warnings.append(SMALL)
    return warnings


def check_cutout(cutout: Image.Image) -> list[str]:
    """Same, from the RGBA image the cutout service returned."""
    return check_alpha(alpha_array(cutout))


def placed_low(centre: float, placement: Placement = DEFAULT_PLACEMENT) -> bool:
    """A face's centre more than PLACED_BY below where the rule puts it."""
    return centre > placement.face_y + PLACED_BY


def placed_high(centre: float, placement: Placement = DEFAULT_PLACEMENT) -> bool:
    """A face's centre more than PLACED_BY above where the rule puts it."""
    return centre < placement.face_y - PLACED_BY


def check_spot(
    spot: Spot,
    size: tuple[int, int],
    bottom: float | None = None,
    placement: Placement = DEFAULT_PLACEMENT,
) -> list[str]:
    """Where the person landed on a picture of `size`, as warnings, in a fixed order.

    `bottom` is how far down the picture the person reaches, in its pixels:
    compose.lowest(). Without it, the bottom of the cutout the spot places.
    `placement` is the rule's, whose face_y the face is measured from.
    """
    height = size[1]
    _, top, _, face_h = spot.face
    centre = (top + face_h / 2) / height
    tall = face_h / height
    reach = (bottom if bottom is not None else spot.top + spot.height) / height
    warnings = []
    if placed_low(centre, placement):
        warnings.append(PLACED_LOW)
    if placed_high(centre, placement):
        warnings.append(PLACED_HIGH)
    if tall < SMALL_FACE:
        warnings.append(FACE_SMALL)
    if tall > LARGE_FACE:
        warnings.append(FACE_LARGE)
    if reach < FLOATS_AT:
        warnings.append(FLOATS)
    if spot.grown and tall > TIGHT_FACE:
        warnings.append(TIGHT)
    return warnings


def check_face(
    cutout: Image.Image | None,
    face: Box | None,
    sent: bool = True,
    placement: Placement = DEFAULT_PLACEMENT,
) -> list[str]:
    """What the face says about this photo on the card, after compose.

    `sent` is whether the post had a face field. The browser always sends one,
    a box or null, and cardctl never does. A null, or a box that is not on the
    person, is "no face found". Then, whenever there is a person on the card,
    where they landed: their face, the browser's box or the top of the
    silhouette that stood in for it, through compose's own arithmetic, and how
    far down the picture they reach, without drawing the card.

    `placement` is the rule compose placed them by, the default one on every
    card.
    """
    person = find_person(cutout, face) if cutout is not None else None
    warnings = []
    if sent and (face is None or (person is not None and person.estimated)):
        warnings.append(NO_FACE)
    if person is not None:
        spot = place_person(person, PICTURE, placement)
        warnings += check_spot(spot, PICTURE, lowest(person, spot, PICTURE), placement)
    return warnings


__all__ = [
    "CUT_OFF",
    "EDGE_FRACTION",
    "FACE_LARGE",
    "FACE_SMALL",
    "FLOATS",
    "FLOATS_AT",
    "LARGE_FACE",
    "MIN_COVERAGE",
    "MIN_HEIGHT_FRACTION",
    "NO_FACE",
    "NO_SUBJECT",
    "OPAQUE",
    "PLACED_BY",
    "PLACED_HIGH",
    "PLACED_LOW",
    "SMALL",
    "SMALL_FACE",
    "TIGHT",
    "TIGHT_FACE",
    "alpha_array",
    "check_alpha",
    "check_cutout",
    "check_face",
    "check_spot",
    "placed_high",
    "placed_low",
]
