"""What happens to an uploaded photo before anything else touches it.

Four things, in order:

  size      anything over MAX_UPLOAD_MB is refused before it is decoded
  decode    Pillow has to be able to open it, or it is not a photo
  orient    the EXIF orientation tag is applied, then every EXIF tag is dropped,
            including the GPS block a phone camera writes
  downscale the long side comes down to MAX_PHOTO_PX

The result is re-encoded as JPEG, so the bytes that reach the cutout service are
bytes this module produced, not bytes a stranger uploaded. It comes back with the
size it was stored at and the scale the downscale applied, because the face box
the browser sends is in the pixels of the photo it sent, and has to be scaled the
same way.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

# Pillow's guard against a small file that decodes to an enormous bitmap. The
# limit it ships with is fine, but a decompression bomb should be a 400 here,
# not a warning in a log.
Image.MAX_IMAGE_PIXELS = 80_000_000

JPEG_QUALITY = 92


class PhotoError(ValueError):
    """The upload is not usable. The message is safe to show the caller."""


@dataclass(frozen=True)
class Prepared:
    """A photo ready to store, and how much it was shrunk to get there."""

    data: bytes  # JPEG, with no EXIF
    size: tuple[int, int]  # width and height as stored
    # Stored pixels per uploaded pixel, 1.0 when the photo was small enough
    # already. The long side comes down to exactly max_px, and Pillow rounds the
    # short side, so round(v * scale) lands a point on the photo's edge on the
    # stored photo's edge.
    scale: float = 1.0


def prepare(raw: bytes, *, max_bytes: int, max_px: int) -> Prepared:
    """Validate, strip, downscale. Returns the JPEG and its scale, or raises PhotoError."""
    if not raw:
        raise PhotoError("The photo is empty.")
    if len(raw) > max_bytes:
        # The route reads one byte past the limit and no further, so the size of
        # the whole photo is not known here.
        raise PhotoError(f"The photo is over the {max_bytes / 1_000_000:g} MB limit.")

    try:
        with Image.open(io.BytesIO(raw)) as opened:
            opened.load()
            # exif_transpose applies the orientation tag and removes it. The
            # copy that comes back carries no other EXIF either, because the
            # JPEG below is written without an exif block.
            image = ImageOps.exif_transpose(opened) or opened
            image = image.convert("RGB")
    except UnidentifiedImageError as exc:
        raise PhotoError("That file is not a photo. Send a JPEG, PNG or WebP.") from exc
    except Image.DecompressionBombError as exc:
        raise PhotoError("The photo has too many pixels to open. Send a smaller one.") from exc
    except OSError as exc:
        message = f"The photo could not be opened ({exc}). Save it again as a JPEG and send that."
        raise PhotoError(message) from exc

    scale = 1.0
    if max(image.size) > max_px:
        scale = max_px / max(image.size)
        image.thumbnail((max_px, max_px), Image.LANCZOS)

    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=JPEG_QUALITY, optimize=True)
    return Prepared(data=buffer.getvalue(), size=image.size, scale=scale)


__all__ = ["JPEG_QUALITY", "PhotoError", "Prepared", "prepare"]
