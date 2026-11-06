"""Where the person goes on the picture. The face decides.

The person stands on the bottom edge of the picture, and the face decides the
rest. Its box, brow to chin as the browser's face detector draws it, is 14
percent of the picture's height, with its centre 27 percent of the way down
and halfway across. A photo cut off at the waist or the chest ends above the
bottom edge at that size, so the face grows until the person reaches it, up to
30 percent, and never so far that the top of the head leaves the picture. The
width never shrinks the person: a bat or an elbow that does not fit leaves the
picture at the side, and the legs leave it at the bottom, the way a real card
crops a photo.

Everything here is arithmetic on sizes and boxes, or a measurement of an alpha
channel, so the rule is tested without drawing anything. compose.py draws.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from PIL import Image

from card.matte import HALF

Box = tuple[float, float, float, float]
"""A face box: x, y, width, height, in the pixels of the image it was found in."""

SIDES = ("left", "top", "right", "bottom")

# The face box becomes FACE_H of the picture's height, with its centre FACE_Y
# of the way down and halfway across.
FACE_H = 0.14
FACE_Y = 0.27

# Never enlarge a cutout more than this to make the face FACE_H, or it turns to
# mush. A face that would need more comes out smaller, still centred at FACE_Y.
MAX_SCALE = 3.0

# The person stands on the bottom edge. When the cutout ends above it, the face
# grows until the cutout reaches the edge, up to FACE_MOST of the picture's
# height, whatever the scale. When even that falls short, the cutout is moved
# down onto the edge, and the face with it.
FACE_MOST = 0.30

# The top of the head may rise no higher than this, as a fraction of the
# picture's height. At 0 the head is never cut by the top edge. A face that
# grows to reach the bottom edge stops growing there, and a person whose head
# is higher than that at any size moves down.
HEAD_TOP = 0.0

# With no face box from the browser, the top of the silhouette stands in for
# one: the top 15 percent of its height is the head, as wide as the silhouette
# is across those rows.
HEAD_BAND = 0.15

# The photo cut the person off where they run into its border: solid pixels in
# its outermost CUT_PX rows or columns. The matte's own soft edge can leave the
# very last pixel short of solid where a body runs off the photo.
CUT_PX = 2

Cut = tuple[str, int, int]
"""Where the photo cut the person off: a side, and the stretch of it they run
into, as the first and one past the last pixel along that side."""


@dataclass(frozen=True)
class Placement:
    """Where the face goes, as fractions of the picture's height."""

    face_h: float = FACE_H
    face_y: float = FACE_Y
    max_scale: float = MAX_SCALE
    face_most: float = FACE_MOST
    head_top: float = HEAD_TOP


DEFAULT_PLACEMENT = Placement()


@dataclass(frozen=True)
class Spot:
    """Where place() puts a cutout.

    Scaled by `scale` to width x height, with its top left corner at (left, top)
    in the picture. Either can be negative, and the cutout can run past the
    right or the bottom edge: the picture crops what falls outside it. `face` is
    the face box where it lands, in the picture's pixels. `grown` is True when
    the scale grew past the face's own to put the person on the bottom edge.
    """

    scale: float
    width: int
    height: int
    left: int
    top: int
    face: Box
    grown: bool = False


def place(
    cutout_size: tuple[int, int],
    face: Box,
    size: tuple[int, int],
    placement: Placement = DEFAULT_PLACEMENT,
    crown: float | None = None,
) -> Spot:
    """The rule, as arithmetic, so it is tested without an image.

    `face` is in the pixels of the cutout, which is cropped to its alpha box,
    so the cutout's bottom row is the lowest part of the person. `crown` is
    the top of the head in the same pixels, crown() of the cutout, and the top
    of the face box when it is not given.

    1. The scale makes the face face_h of the picture's height, and goes no
       further than max_scale. The face's centre goes to face_y of the height
       and half the width.
    2. If the bottom of the cutout then ends above the bottom of the picture,
       the scale grows until it reaches it, as far as a face face_most tall,
       and as far as the person fits, from the top of the head down, between
       head_top and the bottom edge.
    3. If it still ends above it, the cutout is moved down onto the edge, and
       the face lands lower than face_y. When the fit stopped the growth, the
       top of the head lands on head_top.
    4. If the top of the head is above head_top all the same, the cutout is
       moved down until it is not.

    The width never shrinks the person, and nothing moves them sideways.
    """
    width, height = cutout_size
    frame_w, frame_h = size
    x, y, face_w, face_h = face
    centre = y + face_h / 2
    crown = y if crown is None else min(crown, y)
    least = min(placement.face_h * frame_h / face_h, placement.max_scale)
    scale = least
    # How much of the cutout hangs below the face's centre, in its own pixels.
    below = height - centre
    if placement.face_y * frame_h + below * scale < frame_h:
        reach = (1 - placement.face_y) * frame_h / below if below > 0 else math.inf
        most = placement.face_most * frame_h / face_h
        fit = (1 - placement.head_top) * frame_h / (height - crown) if height > crown else math.inf
        scale = max(least, min(reach, most, fit))
    tall = max(1, round(height * scale))
    wide = max(1, round(width * scale))
    left = round(frame_w / 2 - (x + face_w / 2) * scale)
    # On the edge, never a pixel above it: rounding the scaled height can. And
    # never with the head above head_top.
    top = max(
        round(placement.face_y * frame_h - centre * scale),
        frame_h - tall,
        math.ceil(placement.head_top * frame_h - crown * scale),
    )
    return Spot(
        scale,
        wide,
        tall,
        left,
        top,
        (left + x * scale, top + y * scale, face_w * scale, face_h * scale),
        scale > least,
    )


def estimate_face(alpha: Image.Image) -> Box | None:
    """A face box for a cutout the browser found no face on, in the alpha's pixels.

    The top HEAD_BAND of the silhouette's height is the head, as wide as the
    silhouette is across those rows. None when nobody is there.
    """
    values = np.asarray(alpha.convert("L"))
    solid = values >= HALF
    if not solid.any():
        solid = values > 0
        if not solid.any():
            return None
    rows = np.flatnonzero(solid.any(axis=1))
    top = int(rows[0])
    band = max(1, round((int(rows[-1]) + 1 - top) * HEAD_BAND))
    columns = np.flatnonzero(solid[top : top + band].any(axis=0))
    return (float(columns[0]), float(top), float(columns[-1] + 1 - columns[0]), float(band))


def crown(alpha: Image.Image, face: Box) -> float:
    """The top of the head: the first solid row over the face box's columns.

    In the alpha's pixels. Hair above the brow counts, and so does a hat. A
    bat held up beside the head does not. The top of the face box when nothing
    solid is over it.
    """
    solid = np.asarray(alpha.convert("L")) >= HALF
    x, y, face_w, _ = face
    first = max(0, math.floor(x))
    last = min(solid.shape[1], math.ceil(x + face_w))
    rows = np.flatnonzero(solid[:, first:last].any(axis=1)) if last > first else []
    return min(float(rows[0]), float(y)) if len(rows) else float(y)


def cut_lines(alpha: Image.Image) -> tuple[Cut, ...]:
    """Each stretch of the photo's border the person runs into, from the photo's alpha.

    An elbow whose outline curves back inside the photo is not cut off, however
    close it comes to the edge. A stretch shorter than CUT_PX is noise.
    """
    solid = np.asarray(alpha.convert("L")) >= HALF
    edges = {
        "left": solid[:, :CUT_PX].any(axis=1),
        "top": solid[:CUT_PX, :].any(axis=0),
        "right": solid[:, -CUT_PX:].any(axis=1),
        "bottom": solid[-CUT_PX:, :].any(axis=0),
    }
    cuts = []
    for side in SIDES:
        line = np.concatenate(([0], edges[side].astype(np.int8), [0]))
        changes = np.flatnonzero(np.diff(line))
        for start, end in zip(changes[::2], changes[1::2], strict=True):
            if end - start >= CUT_PX:
                cuts.append((side, int(start), int(end)))
    return tuple(cuts)


def shift_face(face: Box | None, box: tuple[int, int, int, int] | None) -> Box | None:
    """Move a face box into the pixels of a crop whose top left corner is box's.

    The cutout is cropped to its alpha box before anything else happens to it,
    and the face box arrives in the pixels of the whole photo.
    """
    if face is None or box is None:
        return face
    x, y, width, height = face
    return (x - box[0], y - box[1], width, height)


def face_on(face: Box | None, size: tuple[int, int]) -> bool:
    """True when at least half of the face box lies on a cutout of this size.

    A box off the cutout is a face the matte did not keep: a picture on the
    wall, someone in the background. Neither the placement nor the photo QA
    should use it.
    """
    if face is None:
        return False
    x, y, face_w, face_h = face
    if face_w <= 0 or face_h <= 0:
        return False
    width, height = size
    on_cutout = max(0.0, min(x + face_w, width) - max(x, 0.0)) * max(
        0.0, min(y + face_h, height) - max(y, 0.0)
    )
    return on_cutout >= 0.5 * face_w * face_h


__all__ = [
    "CUT_PX",
    "DEFAULT_PLACEMENT",
    "FACE_H",
    "FACE_MOST",
    "FACE_Y",
    "HEAD_BAND",
    "HEAD_TOP",
    "MAX_SCALE",
    "SIDES",
    "Box",
    "Cut",
    "Placement",
    "Spot",
    "crown",
    "cut_lines",
    "estimate_face",
    "face_on",
    "place",
    "shift_face",
]
