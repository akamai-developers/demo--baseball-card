"""Put the cutout over the art with Pillow, and return one flat image.

OpenCV blurs the shadow. Everything else is Pillow and numpy.

The result is the only picture on the front of the card. Chromium draws the
frame, the name plate and every word round it. Nothing here writes text,
because an image model cannot spell.

placement.py says where the person goes. Three things here make a pasted
cutout stop looking pasted:

  fade      a photo that cut the person off at a side or at the top ends in a
            straight line there. The person fades out toward it, where it
            lands on the picture
  edge      a matte's outer pixels are part wall. Pasted over a night stadium,
            a white wall shows as a light line round the head. The outer 2 px
            take the colour of the person just inside them instead
  ground    a soft dark pool where the person meets the ground, plus a wider
            halo behind them

Nothing here is allowed to touch the inside of the subject. The face is a real
photograph and that is the whole reason this pipeline exists, so the matte is
sealed before anything is drawn, the edge clean-up stops 2 px inside the
silhouette, and the shadow is masked out wherever the subject covers it. That
holds for any matte handed in, the real BiRefNet one included, not just the one
the mock service makes.

There is no light painted round the person. A rim light read as a light line
round the head on real photos, the same line the edge clean-up removes, and
tests/unit/test_card_compose.py fails on either.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image, ImageDraw

from card import PICTURE_H, PICTURE_W
from card.matte import HALF, fill_holes
from card.placement import (
    DEFAULT_PLACEMENT,
    Box,
    Cut,
    Placement,
    Spot,
    crown,
    cut_lines,
    estimate_face,
    face_on,
    place,
    shift_face,
)

PICTURE = (PICTURE_W, PICTURE_H)

# How far the person fades out from a place the photo cut them off, as a
# fraction of the picture's height.
FADE = 0.08

# The fade stops this many face heights short of the face box on every side, so
# a photo cut off just under the chin fades less, and never fades the face.
FACE_GUARD = 0.25

# How much the shadow darkens the art where it is darkest: 42 percent. The
# same on a night stadium and a day ballpark.
SHADOW = 0.42


@dataclass(frozen=True)
class Person:
    """The person in a cutout, as the card places them.

    image      edge cleaned and cropped to the alpha box
    box        where the crop came from, in the photo's pixels
    photo      the photo's width and height
    face       the face box in the crop's pixels: the browser's, or estimated
    estimated  True when the browser sent no box, or one that is not on the person
    cut        where the photo's border cut the person off
    crown      the top of the head in the crop's pixels: placement.crown()
    """

    image: Image.Image
    box: tuple[int, int, int, int]
    photo: tuple[int, int]
    face: Box
    estimated: bool
    cut: tuple[Cut, ...]
    crown: float


def find_person(cutout: Image.Image, face: Box | None = None) -> Person | None:
    """The person in `cutout`, ready to place. None when nobody is in it.

    `face` is the browser's box in the photo's pixels, which the cutout shares.
    """
    trimmed = trim(cutout)
    if trimmed is None:
        return None
    image, box = trimmed
    head = shift_face(face, box)
    estimated = not face_on(head, image.size)
    if estimated:
        head = estimate_face(image.getchannel("A"))
        if head is None:
            return None
    cut = cut_lines(cutout.convert("RGBA").getchannel("A"))
    return Person(image, box, cutout.size, head, estimated, cut, crown(image.getchannel("A"), head))


def place_person(person: Person, size: tuple[int, int], placement: Placement) -> Spot:
    """place() for a person find_person() found, with the top of their head."""
    return place(person.image.size, person.face, size, placement, person.crown)


def fade_cuts(person: Person, spot: Spot, size: tuple[int, int]) -> Image.Image:
    """The person's picture, faded out where the photo cut them off.

    A photo that cut someone off at the side ends in a straight line down their
    arm, and one that cut off the top of the head in a line across it. Each
    pixel fades with its distance from the nearest stretch of border the person
    runs into, over FADE of the picture's height on the card, so a corner fades
    round and an arm that only grazed the edge fades only there.

    Only a cut that lands inside the picture fades. The person stands on the
    bottom edge, so a cut at the bottom of the photo lands on that edge or past
    it, the picture crops it there, and the person keeps every pixel. And no
    fade reaches the face: each one stops FACE_GUARD face heights short of the
    face box.
    """
    frame_w, frame_h = size
    x0, y0 = person.box[0], person.box[1]
    photo_w, photo_h = person.photo
    scale = spot.scale
    # Where each edge of the photo lands on the picture. One within a pixel of
    # the picture's own edge is on it: place() works in whole pixels.
    photo_left = spot.left - x0 * scale
    photo_top = spot.top - y0 * scale
    shows = {
        "left": photo_left > 1,
        "top": photo_top > 1,
        "right": photo_left + photo_w * scale < frame_w - 1,
        "bottom": photo_top + photo_h * scale < frame_h - 1,
    }
    # How far each side of the photo is from the face and its guard, in the
    # photo's pixels. A fade from that side may reach no further, and where the
    # face is at the edge there is no room and the cut stays a cut.
    face_x, face_y, face_w, face_h = person.face
    guard = FACE_GUARD * face_h
    room = {
        "left": x0 + face_x - guard,
        "top": y0 + face_y - guard,
        "right": photo_w - (x0 + face_x + face_w + guard),
        "bottom": photo_h - (y0 + face_y + face_h + guard),
    }
    fades = [
        (side, start, end, min(FADE * frame_h / scale, room[side]))
        for side, start, end in person.cut
        if shows[side]
    ]
    fades = [fade for fade in fades if fade[3] > 0]
    if not fades:
        return person.image

    width, height = person.image.size
    # Pixel centres, in the photo's pixels.
    across = np.arange(width, dtype=np.float32)[None, :] + 0.5 + x0
    down = np.arange(height, dtype=np.float32)[:, None] + 0.5 + y0
    inward = {"left": across, "top": down, "right": photo_w - across, "bottom": photo_h - down}
    keep = np.ones((height, width), dtype=np.float32)
    for side, start, end, reach in fades:
        along = down if side in ("left", "right") else across
        beyond = np.maximum(np.maximum(start - along, along - end), 0.0)
        ramp = np.clip(np.hypot(beyond, inward[side]) / reach, 0.0, 1.0)
        keep = np.minimum(keep, ramp * ramp * (3.0 - 2.0 * ramp))
    alpha = np.asarray(person.image.getchannel("A"), dtype=np.float32) * keep
    faded = person.image.copy()
    faded.putalpha(Image.fromarray(np.round(alpha).astype(np.uint8), "L"))
    return faded


def lowest(person: Person, spot: Spot, size: tuple[int, int]) -> float:
    """How far down the picture the person reaches, in its pixels.

    The bottom of their lowest solid row on the picture, after the fade: a faint
    fringe is not the person, and neither is a part cropped off at the side.
    The picture's height when they run past its bottom, and the spot's top
    when nothing solid lands on it.
    """
    alpha = np.asarray(fade_cuts(person, spot, size).getchannel("A"))
    step_x = spot.width / alpha.shape[1]
    first = max(0, math.floor(-spot.left / step_x))
    last = min(alpha.shape[1], math.ceil((size[0] - spot.left) / step_x))
    rows = np.flatnonzero((alpha[:, first:last] >= HALF).any(axis=1))
    if not rows.size:
        return float(spot.top)
    return min(spot.top + (int(rows[-1]) + 1) * spot.height / alpha.shape[0], float(size[1]))


def on_picture(
    image: Image.Image, spot: Spot, size: tuple[int, int]
) -> tuple[Image.Image, tuple[int, int]] | None:
    """The part of the scaled cutout that lands on the picture, and where it goes.

    Only that part is resized, from the matching box of the unscaled cutout, so
    a cutout enlarged 3 times with half of it off the picture costs what the
    picture costs. None when none of it lands.
    """
    frame_w, frame_h = size
    left, top = max(spot.left, 0), max(spot.top, 0)
    right = min(spot.left + spot.width, frame_w)
    bottom = min(spot.top + spot.height, frame_h)
    if right <= left or bottom <= top:
        return None
    step_x = image.width / spot.width
    step_y = image.height / spot.height
    source = (
        (left - spot.left) * step_x,
        (top - spot.top) * step_y,
        (right - spot.left) * step_x,
        (bottom - spot.top) * step_y,
    )
    shown = image.resize((right - left, bottom - top), Image.LANCZOS, box=source)
    return shown, (left, top)


# -- the edge ------------------------------------------------------------------

# How far in from the 50 percent line the edge clean-up reaches, in the cutout's
# own pixels. Further in than this the photo is left exactly as it was.
EDGE_PX = 2

# How much steeper the soft edge is made about the 50 percent line. BiRefNet
# predicts at 1024 px and its edge is scaled with the photo, so on a phone photo
# the ramp from wall to person is 4 or 5 px wide, and on a 400 px photo enlarged
# 3 times it reads as a blur round the head. Twice as steep is about 2 px.
EDGE_STEEPNESS = 2.0


def _erode(mask: np.ndarray, steps: int) -> np.ndarray:
    """Shrink a mask by `steps` pixels in all eight directions.

    The border of the picture is not an edge. A person who runs off the bottom
    of the photo is still solid there, so the border row is repeated outward
    instead of being read as background.
    """
    height, width = mask.shape
    for _ in range(steps):
        padded = np.pad(mask, 1, mode="edge")
        shrunk = mask.copy()
        for dy in (0, 1, 2):
            for dx in (0, 1, 2):
                shrunk &= padded[dy : dy + height, dx : dx + width]
        mask = shrunk
    return mask


def _seal(alpha: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """The sealed alpha, and the core: the person more than EDGE_PX inside the line."""
    solid = alpha >= HALF
    person = fill_holes(solid)
    core = _erode(person, EDGE_PX)
    # The core and any hole the fill closed are solid. The edge keeps its own
    # alpha, so fine hair stays soft.
    sealed = np.maximum(alpha, (core | (person & ~solid)) * np.uint8(255))
    return sealed, core


def seal_alpha(alpha: Image.Image) -> Image.Image:
    """Make the inside of the silhouette opaque, whatever the matte says.

    Two failures put the background through a face, and this closes both:

    A hole. Any transparent pixel the border cannot reach is enclosed by the
    subject, so it is filled.

    A weak matte. Dark hair, an eye, the shadow under a chin: a segmentation
    model that is unsure gives them alpha 0.6 and the stadium comes through at 40
    percent. Everything more than EDGE_PX inside the 50 percent line is forced to
    255, so only the outer pixel or two is ever part transparent.

    The line is 50 percent and not "anything above zero" on purpose. BiRefNet
    hedges at 5 to 60 percent in the notch where an ear meets the head, and a
    seal that counted those pixels as the person made the notch solid, which
    put a fleck of wall beside each ear.

    card.matte.fill_holes pads a ring of background on before the flood fill, so
    a subject that touches every edge of its crop does not get eaten.
    """
    sealed, _ = _seal(np.asarray(alpha.convert("L")))
    return Image.fromarray(sealed, "L")


def _grow_colour(rgb: np.ndarray, known: np.ndarray, wanted: np.ndarray) -> np.ndarray:
    """Give each wanted pixel the average colour of its known neighbours, a ring at a time.

    Only the wanted pixels are visited, so the cost is the length of the edge
    and not the size of the photo. A wanted pixel that no ring reaches keeps
    its colour.
    """
    height, width = known.shape
    stride = width + 2
    colour = np.zeros((height + 2, stride, 3), dtype=np.float32)
    colour[1:-1, 1:-1] = rgb
    colour = colour.reshape(-1, 3)
    have = np.pad(known, 1).reshape(-1)
    left = np.flatnonzero(np.pad(wanted & ~known, 1))
    around = np.array([dy * stride + dx for dy in (-1, 0, 1) for dx in (-1, 0, 1) if dy or dx])

    while left.size:
        near = left[:, None] + around[None, :]
        weight = have[near]
        count = weight.sum(axis=1)
        ready = count > 0
        if not ready.any():
            break
        total = (colour[near] * weight[..., None]).sum(axis=1)
        filled = left[ready]
        colour[filled] = total[ready] / count[ready, None]
        have[filled] = True
        left = left[~ready]
    return colour.reshape(height + 2, stride, 3)[1:-1, 1:-1]


def clean_edge(cutout: Image.Image) -> Image.Image:
    """Seal the matte, take the wall out of its edge, and make the edge crisp.

    The outer pixels of any matte are part wall: the photo blends the two
    there, and the matte's soft values say how much of each. Solving that blend
    for the subject's colour against the wall's works only when the alpha is
    the true coverage. A refined matte's is not. Its feather reaches pixels
    that are all wall, and those solve straight back to the wall. Measured on
    real photos, the solve left the light line exactly where it was.

    So the edge keeps no colour of its own. Every pixel outside the core takes
    the average colour of the person just inside it, grown outward a ring at a
    time, and no wall is left to show whatever the matte got wrong. Then the
    soft ramp is made EDGE_STEEPNESS times steeper about the 50 percent line.

    RGBA in, RGBA out, the same size. The core, and so the face, is untouched.
    """
    rgba = cutout.convert("RGBA")
    box = rgba.getchannel("A").getbbox()
    if box is None:
        return rgba

    # Only the part with a person in it, plus room for the edge, is worked on.
    # Where the crop stops short of the photo's border, it stops on background.
    margin = EDGE_PX + 2
    left, top = max(box[0] - margin, 0), max(box[1] - margin, 0)
    right, bottom = min(box[2] + margin, rgba.width), min(box[3] + margin, rgba.height)
    work = rgba.crop((left, top, right, bottom))

    sealed, core = _seal(np.asarray(work.getchannel("A")))
    ramp = (sealed.astype(np.float32) / 255.0 - 0.5) * EDGE_STEEPNESS + 0.5
    alpha = np.round(np.clip(ramp, 0.0, 1.0) * 255.0).astype(np.uint8)

    rgb = np.asarray(work.convert("RGB"), dtype=np.float32)
    edge = (alpha > 0) & ~core
    grown = _grow_colour(rgb, core, edge)
    pixels = np.where(edge[..., None], grown, rgb)

    cleaned = Image.fromarray(np.clip(pixels + 0.5, 0, 255).astype(np.uint8), "RGB")
    cleaned.putalpha(Image.fromarray(alpha, "L"))
    rgba.paste(cleaned, (left, top))
    return rgba


def trim(cutout: Image.Image) -> tuple[Image.Image, tuple[int, int, int, int]] | None:
    """The person as the card uses them: edge cleaned, cropped to the alpha box.

    Returns the crop and the box it came from in the cutout's pixels, which
    are the photo's, or None when there is nobody in it.
    """
    cleaned = clean_edge(cutout)
    box = cleaned.getchannel("A").getbbox()
    if box is None:
        return None
    return cleaned.crop(box), box


# -- the picture ---------------------------------------------------------------


def cover(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Scale to fill `size` and centre-crop the overflow. Same rule as CSS object-fit: cover."""
    target_w, target_h = size
    scale = max(target_w / image.width, target_h / image.height)
    scaled = image.resize(
        (max(target_w, round(image.width * scale)), max(target_h, round(image.height * scale))),
        Image.LANCZOS,
    )
    left = (scaled.width - target_w) // 2
    top = (scaled.height - target_h) // 2
    return scaled.crop((left, top, left + target_w, top + target_h))


def _box_widths(sigma: float, passes: int = 3) -> list[int]:
    """Odd box widths whose passes spread as a Gaussian of `sigma` does.

    Two neighbouring widths, so the spread comes out close even for a small
    sigma, where one width rounded for all three passes is several percent off.
    """
    ideal = math.sqrt(12 * sigma * sigma / passes + 1)
    low = math.floor(ideal)
    if low % 2 == 0:
        low -= 1
    narrow = round(
        (12 * sigma * sigma - passes * low * low - 4 * passes * low - 3 * passes) / (-4 * low - 4)
    )
    return [low if index < narrow else low + 2 for index in range(passes)]


def _blur(mask: np.ndarray, sigma: float) -> np.ndarray:
    """Pillow's Gaussian blur, in OpenCV: three box passes with the same spread.

    Pillow blurs by three box passes whose widths give a Gaussian of standard
    deviation `sigma`, repeating the edge pixels past the border. OpenCV's box
    filter does the same in a tenth of the time. For a person 300 px wide or
    more on the card, and the face rule makes most people 500 to 1,400, it is
    within one level of Pillow's result. From 100 to 300 px it is within three,
    and under that, where the blur is a few pixels wide, within eight. A true
    Gaussian this wide, sigma 30 to 60 px, is slower than either.
    """
    for width in _box_widths(sigma):
        mask = cv2.blur(mask, (width, width), borderType=cv2.BORDER_REPLICATE)
    return mask


def _on_frame(alpha: np.ndarray, size: tuple[int, int], left: int, top: int) -> np.ndarray:
    """`alpha` on an empty frame of `size`, its top left corner at (left, top)."""
    frame_w, frame_h = size
    frame = np.zeros((frame_h, frame_w), dtype=np.uint8)
    height, width = alpha.shape
    x0, y0 = max(left, 0), max(top, 0)
    x1, y1 = min(left + width, frame_w), min(top + height, frame_h)
    if x1 > x0 and y1 > y0:
        frame[y0:y1, x0:x1] = alpha[y0 - top : y1 - top, x0 - left : x1 - left]
    return frame


def _shadow_mask(
    alpha: Image.Image, size: tuple[int, int], left: int, top: int
) -> tuple[np.ndarray, int]:
    """A pool where the subject meets the ground, and a halo that fades out going up.

    A halo around the whole silhouette reads as a cheap drop shadow, worst of all
    behind the head against a bright sky. Ramping it off above the waist leaves
    the part that does the work, the darkening where the subject meets the
    ground. A subject who does not reach the bottom of the picture is not
    standing on anything, so there is no pool under them.

    Returns the shadow from its first row down, 0 to 255, and that row. Every
    row above it is 0, so the caller darkens only from there.
    """
    frame_w, frame_h = size
    width, height = alpha.size
    drop = round(width * 0.025)
    sigma = width * 0.045
    matte = np.asarray(alpha)
    halo = _blur(_on_frame(matte, size, left + drop, top + drop), sigma)

    ground = None
    if top + height >= frame_h:
        drawn = Image.new("L", (frame_w, frame_h), 0)
        centre_x = left + width // 2
        radius_x = width * 0.46
        radius_y = width * 0.085
        ImageDraw.Draw(drawn).ellipse(
            [centre_x - radius_x, frame_h - radius_y, centre_x + radius_x, frame_h + radius_y],
            fill=255,
        )
        ground = _blur(np.asarray(drawn), sigma)

    # The halo is ramped to nothing above fade_from, and the pool only reaches
    # a little way up from the floor. The shadow starts at whichever is higher.
    fade_from = top + height * 0.35
    fade_to = top + height * 0.90
    first = min(max(math.floor(fade_from), 0), frame_h)
    if ground is not None:
        pooled = np.flatnonzero(ground.any(axis=1))
        if pooled.size:
            first = min(first, int(pooled[0]))
    rows = np.arange(first, frame_h, dtype=np.float32)[:, None]
    ramp = np.clip((rows - fade_from) / max(fade_to - fade_from, 1.0), 0.0, 1.0)
    pool = halo[first:].astype(np.float32) * ramp
    if ground is not None:
        pool = np.maximum(pool, ground[first:])

    # The shadow layer is painted on the art and the subject goes on top of it,
    # so it is already behind them. Taking the subject's own alpha back out makes
    # that true for the part-transparent pixels round the edge as well, which are
    # the only ones the subject does not fully cover.
    in_front = _on_frame(matte, size, left, top)[first:].astype(np.float32) / 255.0
    return (pool * (1.0 - in_front)).astype(np.uint8), first


def compose_hero(
    art: Image.Image,
    cutout: Image.Image | None,
    size: tuple[int, int] = PICTURE,
    placement: Placement = DEFAULT_PLACEMENT,
    face: Box | None = None,
) -> Image.Image:
    """Return the finished front picture: the art, the shadow, the person.

    `size` is the card's picture unless a test asks for another. `face` is the
    browser's face box in the photo's pixels, which the cutout shares. Without
    one, or with one that is not on the person, the top of the silhouette
    stands in for it.
    """
    person = find_person(cutout, face) if cutout is not None else None
    shown = None
    if person is not None:
        spot = place_person(person, size, placement)
        shown = on_picture(fade_cuts(person, spot, size), spot, size)
    if shown is None:
        return cover(art.convert("RGB"), size)
    subject, (left, top) = shown

    base = cover(art.convert("RGB"), size)

    shadow, first = _shadow_mask(subject.getchannel("A"), size, left, top)
    pixels = np.array(base)
    darkened = pixels[first:].astype(np.float32) * (
        1.0 - shadow.astype(np.float32)[..., None] / 255.0 * SHADOW
    )
    pixels[first:] = np.clip(darkened, 0, 255).astype(np.uint8)
    base = Image.fromarray(pixels, "RGB")
    base.paste(subject, (left, top), subject)
    return base


__all__ = [
    "EDGE_PX",
    "FACE_GUARD",
    "FADE",
    "PICTURE",
    "SHADOW",
    "Person",
    "clean_edge",
    "compose_hero",
    "cover",
    "fade_cuts",
    "find_person",
    "lowest",
    "on_picture",
    "place_person",
    "seal_alpha",
    "trim",
]
