"""Stand-ins for the two vision models, written with numpy and Pillow.

`stadium()` draws a ballpark background. It is what the mock art service returns
and what `render.py --fixture` uses, so a laptop can render a card with no GPU
and no weights.

`plain_wall_cutout()` mattes a subject off a plain background by colour distance.
It is what the mock cutout service uses. It only works on the input this demo
asks for, a photo against a plain wall, and it is not trying to be BiRefNet.

Both are deterministic. Same arguments in, same pixels out.
"""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from card.matte import fill_holes, grow, largest_blob, shrink

# Two parks. None for `glow` means a daylight park, so no floodlights.
_NIGHT = {
    "sky": ((5, 10, 32), (30, 56, 104)),
    "stands": (20, 27, 50),
    "wall": (16, 52, 36),
    "grass": ((34, 104, 58), (18, 62, 36)),
    "dirt": (112, 74, 48),
    "glow": (255, 246, 214),
}
_DAY = {
    "sky": ((104, 170, 230), (212, 234, 248)),
    "stands": (82, 92, 108),
    "wall": (126, 46, 36),
    "grass": ((78, 156, 74), (46, 110, 50)),
    "dirt": (176, 124, 78),
    "glow": None,
}

# Colours per style, the park its art prompt asks FLUX for.
_PROC: dict[str, dict] = {"akamai": _NIGHT, "classic": _DAY}

DEFAULT_STYLE = "akamai"


def _vertical_gradient(width: int, height: int, top: tuple, bottom: tuple) -> np.ndarray:
    """An h x w x 3 float array that fades from top to bottom."""
    ramp = np.linspace(0.0, 1.0, height, dtype=np.float32)[:, None, None]
    a = np.asarray(top, dtype=np.float32)[None, None, :]
    b = np.asarray(bottom, dtype=np.float32)[None, None, :]
    return np.repeat(a + (b - a) * ramp, width, axis=1)


def _floodlights(image: Image.Image, palette: dict, horizon: int, stands_top: int) -> Image.Image:
    """Two light towers. The glow is a blurred mask, not a blurred RGBA layer.

    Blurring RGBA directly drags the transparent black into the colour channels
    and the towers come out as dark smudges. Blurring a mask and compositing a
    solid colour through it keeps them bright.
    """
    width, height = image.size
    mask = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(mask)
    for fraction in (0.16, 0.84):
        cx = int(width * fraction)
        cy = int(stands_top - height * 0.10)
        draw.ellipse([cx - 250, cy - 200, cx + 250, cy + 200], fill=40)
        draw.rectangle([cx - 6, cy, cx + 6, horizon], fill=120)
        draw.rounded_rectangle([cx - 86, cy - 50, cx + 86, cy + 18], radius=12, fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(height / 90))
    return Image.composite(Image.new("RGB", (width, height), palette["glow"]), image, mask)


def stadium(
    style: str = DEFAULT_STYLE,
    width: int = 1024,
    height: int = 1408,
    seed: int = 0,
) -> Image.Image:
    """Draw a ballpark background. No text, no logos, no people, same as the real prompt."""
    palette = _PROC.get(style, _PROC[DEFAULT_STYLE])
    rng = np.random.default_rng(seed)

    horizon = int(height * 0.52)
    wall_bottom = int(height * 0.60)
    stands_top = int(horizon - height * 0.17)

    canvas = np.zeros((height, width, 3), dtype=np.float32)
    canvas[:horizon] = _vertical_gradient(width, horizon, *palette["sky"])
    canvas[wall_bottom:] = _vertical_gradient(width, height - wall_bottom, *palette["grass"])
    canvas[horizon:wall_bottom] = np.asarray(palette["wall"], dtype=np.float32)

    image = Image.fromarray(np.clip(canvas, 0, 255).astype(np.uint8), "RGB")
    draw = ImageDraw.Draw(image, "RGBA")

    # Stands above the wall: two decks, with a speckle standing in for a crowd.
    draw.rectangle([0, stands_top, width, horizon], fill=(*palette["stands"], 255))
    deck = stands_top + (horizon - stands_top) // 2
    draw.rectangle([0, deck - 6, width, deck + 6], fill=(0, 0, 0, 120))
    draw.rectangle([0, horizon - 10, width, horizon], fill=(0, 0, 0, 150))
    for _ in range(5200):
        x = int(rng.integers(0, width))
        y = int(rng.integers(stands_top + 8, horizon - 12))
        value = int(rng.integers(90, 210))
        draw.point((x, y), fill=(value, value, value, 120))

    # Mown stripes on the outfield. They converge, which reads as depth.
    stripe = width / 9.0
    for index in range(-3, 13):
        if index % 2:
            continue
        x0 = index * stripe
        draw.polygon(
            [
                (x0, height),
                (x0 + stripe, height),
                (x0 * 0.55 + width * 0.25 + stripe * 0.55, wall_bottom),
                (x0 * 0.55 + width * 0.25, wall_bottom),
            ],
            fill=(255, 255, 255, 26),
        )

    # Infield dirt, a wide shallow arc across the bottom.
    dirt_top = int(height * 0.80)
    draw.ellipse(
        [-width * 0.35, dirt_top, width * 1.35, height * 1.32],
        fill=(*palette["dirt"], 255),
    )

    if palette["glow"]:
        image = _floodlights(image, palette, horizon, stands_top)

    # A real background behind a subject is out of focus.
    image = image.filter(ImageFilter.GaussianBlur(height / 420))

    # Vignette and grain, so the flat areas are not obviously flat.
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    dx = (xx / width - 0.5) * 2.0
    dy = (yy / height - 0.5) * 2.0
    vignette = 1.0 - 0.28 * np.clip(np.hypot(dx, dy) / 1.414, 0, 1) ** 1.6
    pixels = np.asarray(image, dtype=np.float32) * vignette[..., None]
    pixels += rng.normal(0.0, 2.6, pixels.shape).astype(np.float32)
    return Image.fromarray(np.clip(pixels, 0, 255).astype(np.uint8), "RGB")


def _border_colour(pixels: np.ndarray, band: int = 24) -> np.ndarray:
    """Median colour of a ring around the edge. On a plain-wall photo that is the wall."""
    ring = np.concatenate(
        [
            pixels[:band].reshape(-1, 3),
            pixels[-band:].reshape(-1, 3),
            pixels[:, :band].reshape(-1, 3),
            pixels[:, -band:].reshape(-1, 3),
        ]
    )
    return np.median(ring, axis=0)


def _background_model(pixels: np.ndarray, subject: np.ndarray, cells: int = 48) -> np.ndarray:
    """A smooth per-pixel estimate of the wall behind the subject.

    A studio wall is rarely one flat colour. It falls off toward the corners and
    is often brightest right behind the subject, so one median colour leaves a
    halo. This averages the non-subject pixels into a coarse grid, fits a
    quadratic surface per channel to the cells the subject does not cover, and
    uses that surface to say what the wall would have been behind them.
    """
    height, width, _ = pixels.shape
    small = (cells, max(2, round(cells * height / width)))
    weight = (~subject).astype(np.float32)

    def shrink(array: np.ndarray) -> np.ndarray:
        if array.ndim == 3:
            return np.stack([shrink(array[..., c]) for c in range(array.shape[2])], axis=-1)
        source = Image.fromarray(np.ascontiguousarray(array, dtype=np.float32), "F")
        return np.asarray(source.resize(small, Image.BOX), dtype=np.float32)

    total = shrink(pixels * weight[..., None])
    count = shrink(weight)
    known = count > 0.15
    grid = total / np.maximum(count, 1e-6)[..., None]

    grid_h, grid_w = known.shape
    yy, xx = np.mgrid[0:grid_h, 0:grid_w].astype(np.float32)
    xx = xx / max(grid_w - 1, 1) * 2.0 - 1.0
    yy = yy / max(grid_h - 1, 1) * 2.0 - 1.0
    basis = np.stack([np.ones_like(xx), xx, yy, xx * xx, xx * yy, yy * yy], axis=-1)

    if int(known.sum()) >= 3 * basis.shape[-1]:
        surface = np.stack(
            [
                basis @ np.linalg.lstsq(basis[known], grid[known][:, channel], rcond=None)[0]
                for channel in range(3)
            ],
            axis=-1,
        )
    else:
        surface = np.broadcast_to(_border_colour(pixels), grid.shape).copy()

    estimate = np.where(known[..., None], grid, surface)
    as_image = Image.fromarray(np.clip(estimate, 0, 255).astype(np.uint8), "RGB")
    smooth = as_image.resize((width, height), Image.BICUBIC).filter(
        ImageFilter.GaussianBlur(max(width, height) / 140)
    )
    return np.asarray(smooth, dtype=np.float32)


def _unmix(pixels: np.ndarray, background: np.ndarray, soft: Image.Image) -> np.ndarray:
    """Take the wall's colour back out of the part-transparent edge pixels.

    An edge pixel is a mix: colour = alpha * subject + (1 - alpha) * wall. Paste
    it over a night stadium without solving that back and the wall it was cut
    from shows up as a bright fringe around the head. Solving for the subject
    removes the fringe. Below alpha 0.02 the division is meaningless and the
    pixel is invisible anyway, so it is left alone.
    """
    alpha = (np.asarray(soft, dtype=np.float32) / 255.0)[..., None]
    subject = (pixels - (1.0 - alpha) * background) / np.maximum(alpha, 0.12)
    return np.where(alpha > 0.02, np.clip(subject, 0, 255), pixels).astype(np.uint8)


def plain_wall_cutout(image: Image.Image, low: float = 0.075, high: float = 0.17) -> Image.Image:
    """Matte a subject off a plain background. Returns RGBA with a soft edge.

    Pass one finds the subject roughly, using distance from the median edge
    colour. Pass two models the wall as a smooth gradient with the subject
    excluded, and measures distance from that instead, which kills the halo.
    """
    rgb = image.convert("RGB")
    pixels = np.asarray(rgb, dtype=np.float32)

    rough = np.linalg.norm(pixels - _border_colour(pixels), axis=2) / 441.67 > 0.10
    background = _background_model(pixels, rough)

    distance = np.linalg.norm(pixels - background, axis=2) / 441.67
    alpha = np.clip((distance - low) / max(high - low, 1e-6), 0.0, 1.0)

    # Wide enough to bridge the channel between a pair of glasses and the edge of
    # a face, which is the gap that lets the flood fill in and leaves a hole
    # where an eye should be.
    radius = max(4.0, min(rgb.width, rgb.height) * 0.016)

    # Open to drop specks, close to seal those channels, fill what is left
    # enclosed, then throw away anything that is not joined to the body. The
    # result is one clean silhouette with no interior detail and no islands.
    speckless = Image.fromarray((alpha > 0.5).astype(np.uint8) * 255, "L")
    speckless = speckless.filter(ImageFilter.MinFilter(5)).filter(ImageFilter.MaxFilter(5))
    sealed = largest_blob(fill_holes(np.asarray(shrink(grow(speckless, radius), radius)) > 127))
    silhouette = Image.fromarray(sealed.astype(np.uint8) * 255, "L")

    # The silhouette is the matte. Every pixel inside it is opaque and the only
    # soft part is the outer pixel or two, which a 1.2 blur gives.
    #
    # Letting the colour distance go on deciding inside the outline is what put a
    # stadium through the middle of a face. Skin in shadow along a jaw sits close
    # enough to a grey wall that the distance reads as background, and the result
    # is a notch bitten out of a cheek. It buys nothing back: the closing that
    # seals the channel between a pair of glasses and a temple is wider than a
    # strand of hair, so the fine edge this would keep is already gone. The cost
    # is that a gap the closing bridged, between an arm and a torso, comes out as
    # a patch of wall on the subject rather than a hole in them. On the input
    # this asks for, a headshot against a plain wall, that is the better trade.
    soft = silhouette.filter(ImageFilter.GaussianBlur(1.2))

    out = Image.fromarray(_unmix(pixels, background, soft), "RGB").convert("RGBA")
    out.putalpha(soft)
    return out


__all__ = ["DEFAULT_STYLE", "plain_wall_cutout", "stadium"]
