"""Mask arithmetic. Grow, shrink, fill holes, drop islands, refine a matte.

Three places need the same handful of operations on an alpha channel, and they
are the same operations whether the matte came from BiRefNet or from the colour
distance in procedural.py:

  services/cutout  cleans what the model returned before it leaves the service
  card/procedural  builds the mock matte out of these
  card/compose     seals whatever it is handed before it composites

Nothing here knows about a model. A mask goes in, a mask comes out.

Two costs worth knowing. Pillow's rank filters are O(k^2) per pixel, so a 20 px
MaxFilter over a 2048 px photo is seconds, and `grow` and `shrink` use a blur and
a threshold instead. And a Python loop per pixel is seconds too, which is why
the hole filling and the island dropping both work on runs of a row, a handful
per row, instead of on pixels.
"""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageFilter

# The matte's 50 percent line: at or above this alpha a pixel is the person.
HALF = 128


def as_mask(binary: np.ndarray) -> Image.Image:
    """A bool array as an 8-bit Pillow mask."""
    return Image.fromarray(binary.astype(np.uint8) * 255, "L")


def grow(mask: Image.Image, radius: float) -> Image.Image:
    """Dilate by about `radius`. A blur and a low threshold.

    A rank filter wide enough to seal the gap between a pair of glasses and the
    edge of a face is 20 px across, and Pillow's MaxFilter is O(k^2) per pixel.
    A blur costs the same whatever the radius.
    """
    return mask.filter(ImageFilter.GaussianBlur(radius)).point(lambda v: 255 if v >= 64 else 0)


def shrink(mask: Image.Image, radius: float) -> Image.Image:
    """Erode by about `radius`. The same blur with a high threshold."""
    return mask.filter(ImageFilter.GaussianBlur(radius)).point(lambda v: 255 if v >= 192 else 0)


def fill_holes(binary: np.ndarray) -> np.ndarray:
    """Close gaps inside the subject: anything false the border cannot reach.

    The background is split into runs and joined into pieces, the same way
    `largest_blob` joins the subject. A piece with a pixel on any edge of the
    frame is the real background. Every other piece is enclosed by the subject,
    so it is a hole, and it is filled. A subject that runs off an edge of the
    frame does not close anything off against that edge.

    Pieces join through a shared side, not a shared corner, which is what a
    flood fill does. This used to be one: ImageDraw.floodfill, which is a
    Python loop per pixel and took a second over a 1100 px phone photo.
    """
    height, width = binary.shape
    if not height or not width:
        return binary.copy()
    rows, starts, ends, roots, _ = _label_runs(~binary)
    open_to_edge = {
        root
        for row, start, end, root in zip(rows, starts, ends, roots, strict=True)
        if row == 0 or row == height - 1 or start == 0 or end == width
    }
    filled = binary.copy()
    for row, start, end, root in zip(rows, starts, ends, roots, strict=True):
        if root not in open_to_edge:
            filled[row, start:end] = True
    return filled


def _label_runs(binary: np.ndarray) -> tuple[list[int], list[int], list[int], list[int], dict]:
    """Connected components, as runs of true pixels joined row to row.

    Returns the runs (row, start, end), the component each one belongs to, and
    how many pixels each component holds. Rows are collapsed into runs first,
    which is quick because a silhouette has a handful of runs per row rather
    than a few thousand pixels.
    """
    padded = np.zeros((binary.shape[0], binary.shape[1] + 2), dtype=np.int8)
    padded[:, 1:-1] = binary
    change = np.diff(padded, axis=1)
    rows, starts = (array.tolist() for array in np.nonzero(change == 1))
    ends = np.nonzero(change == -1)[1].tolist()

    parent = list(range(len(starts)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    # first[r] is where row r's runs begin, so they are the slice first[r]:first[r+1].
    first = np.searchsorted(rows, np.arange(binary.shape[0] + 1)).tolist()

    for row in range(1, binary.shape[0]):
        here, above = first[row], first[row - 1]
        while here < first[row + 1] and above < first[row]:
            if starts[here] < ends[above] and starts[above] < ends[here]:
                root_here, root_above = find(here), find(above)
                if root_here != root_above:
                    parent[root_above] = root_here
            if ends[here] < ends[above]:
                here += 1
            else:
                above += 1

    roots = [find(index) for index in range(len(starts))]
    sizes: dict[int, int] = {}
    for index, root in enumerate(roots):
        sizes[root] = sizes.get(root, 0) + ends[index] - starts[index]
    return rows, starts, ends, roots, sizes


def _keep(binary: np.ndarray, wanted) -> np.ndarray:
    """Rebuild a mask from the runs whose component `wanted` says to keep."""
    rows, starts, ends, roots, sizes = _label_runs(binary)
    out = np.zeros_like(binary)
    for index, root in enumerate(roots):
        if wanted(sizes[root], sizes):
            out[rows[index], starts[index] : ends[index]] = True
    return out


def largest_blob(binary: np.ndarray) -> np.ndarray:
    """Keep the biggest connected piece and drop every other one.

    A dark patch of floor in one corner, a speck of shadow, a picture frame on
    the wall: anything the matte caught that is not joined to the body goes.
    """
    if not binary.any():
        return binary
    biggest = max(_label_runs(binary)[4].values())
    return _keep(binary, lambda size, _: size == biggest)


def drop_small_islands(binary: np.ndarray, min_fraction: float = 0.05) -> np.ndarray:
    """Keep every piece at least `min_fraction` as big as the biggest one.

    Softer than largest_blob, and the right rule for a segmentation model.
    BiRefNet returns an arm held away from the body as its own component when the
    gap between them is wider than a pixel, and throwing that away loses a hand.
    A speck of shadow is three orders of magnitude smaller than a person, so five
    percent separates them with room to spare.
    """
    if not binary.any():
        return binary
    biggest = max(_label_runs(binary)[4].values())
    floor = biggest * min_fraction
    return _keep(binary, lambda size, _: size >= floor)


def refine_matte(
    mask: Image.Image,
    *,
    feather_px: float = 1.2,
    min_island: float = 0.05,
    halo_px: float = 3.0,
) -> Image.Image:
    """Clean a model's alpha channel. Islands go, holes close, the outline stays soft.

    What this keeps is the point. A segmentation model's best work is the half
    transparent pixels along a hairline, and hard thresholding a matte throws all
    of it away and leaves a cut-out-with-scissors edge. So the soft values
    survive wherever they sit within `halo_px` of the subject, and everything
    else is decided:

      islands   a piece under `min_island` of the biggest one is not the subject.
                It goes, and so does the soft halo around it, or a dropped speck
                leaves a grey smudge where it was.
      holes     a transparent region the border cannot reach is enclosed by the
                subject, so it is filled and forced opaque.
      unsure    anything the model put above 0.5 becomes fully opaque. Dark hair,
                an eye, the shadow under a chin: alpha 0.6 on a face means the
                stadium comes through it at 40 percent, which is the one thing
                this pipeline exists to prevent.

    An empty mask is handed back untouched. The model found nothing, and there is
    no sensible way to invent a subject.
    """
    soft = np.asarray(mask.convert("L"), dtype=np.float32) / 255.0
    solid = soft > 0.5
    if not solid.any():
        return mask.convert("L")

    kept = fill_holes(drop_small_islands(solid, min_island))
    near = np.asarray(grow(as_mask(kept), halo_px), dtype=np.float32) / 255.0
    alpha = np.maximum(soft * near, kept.astype(np.float32))

    out = Image.fromarray(np.clip(alpha * 255.0, 0, 255).astype(np.uint8), "L")
    return out.filter(ImageFilter.GaussianBlur(feather_px)) if feather_px > 0 else out


__all__ = [
    "HALF",
    "as_mask",
    "drop_small_islands",
    "fill_holes",
    "grow",
    "largest_blob",
    "refine_matte",
    "shrink",
]
