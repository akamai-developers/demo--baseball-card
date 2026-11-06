"""What happens to an upload before the pipeline touches it."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from api.photo import PhotoError, Prepared, prepare


def jpeg(size: tuple[int, int], exif: Image.Exif | None = None) -> bytes:
    buffer = io.BytesIO()
    image = Image.new("RGB", size, (140, 150, 160))
    image.save(buffer, "JPEG", exif=exif) if exif else image.save(buffer, "JPEG")
    return buffer.getvalue()


def test_a_normal_photo_comes_back_as_jpeg() -> None:
    out = prepare(jpeg((800, 600)), max_bytes=15_000_000, max_px=2048)
    assert isinstance(out, Prepared)
    with Image.open(io.BytesIO(out.data)) as image:
        assert image.format == "JPEG"
        assert image.size == (800, 600)


def test_a_big_photo_is_downscaled_on_the_long_side() -> None:
    out = prepare(jpeg((4000, 3000)), max_bytes=15_000_000, max_px=2048)
    with Image.open(io.BytesIO(out.data)) as image:
        assert max(image.size) == 2048
        assert image.size == (2048, 1536)


def test_a_small_photo_is_not_enlarged() -> None:
    out = prepare(jpeg((300, 200)), max_bytes=15_000_000, max_px=2048)
    with Image.open(io.BytesIO(out.data)) as image:
        assert image.size == (300, 200)


# -- the scale, which the face box follows -----------------------------------


def test_a_photo_that_fits_is_not_scaled() -> None:
    out = prepare(jpeg((800, 600)), max_bytes=15_000_000, max_px=2048)
    assert (out.size, out.scale) == ((800, 600), 1.0)


def test_the_scale_is_what_the_downscale_applied() -> None:
    out = prepare(jpeg((4000, 3000)), max_bytes=15_000_000, max_px=2048)
    assert out.size == (2048, 1536)
    assert out.scale == 2048 / 4000


def test_the_size_is_the_size_of_the_stored_bytes() -> None:
    out = prepare(jpeg((2000, 3001)), max_bytes=15_000_000, max_px=2048)
    with Image.open(io.BytesIO(out.data)) as image:
        assert image.size == out.size


@pytest.mark.parametrize(
    "size", [(3001, 2000), (3001, 2001), (2000, 3001), (4000, 3000), (5000, 4999), (2049, 17)]
)
def test_the_photo_edges_scale_onto_the_stored_edges(size: tuple[int, int]) -> None:
    """Pillow rounds the short side its own way. A box clamped to the photo the
    browser sent must still fit the photo that is stored, to the pixel."""
    out = prepare(jpeg(size), max_bytes=15_000_000, max_px=2048)
    assert (round(size[0] * out.scale), round(size[1] * out.scale)) == out.size


def test_exif_is_gone() -> None:
    """A phone writes GPS coordinates into a photo. None of it survives this."""
    exif = Image.Exif()
    exif[0x010F] = "SomePhone"  # Make
    exif[0x9286] = "a comment nobody asked for"  # UserComment
    raw = jpeg((600, 400), exif)
    with Image.open(io.BytesIO(raw)) as before:
        assert before.getexif(), "the fixture has no EXIF, so this test proves nothing"

    out = prepare(raw, max_bytes=15_000_000, max_px=2048)
    with Image.open(io.BytesIO(out.data)) as after:
        assert dict(after.getexif()) == {}


def test_the_orientation_tag_is_applied_before_it_is_dropped() -> None:
    """Orientation 6 means the camera was on its side. The pixels get rotated."""
    exif = Image.Exif()
    exif[0x0112] = 6
    out = prepare(jpeg((800, 400), exif), max_bytes=15_000_000, max_px=2048)
    with Image.open(io.BytesIO(out.data)) as image:
        assert image.size == (400, 800)
    # The size a face box is checked against is the rotated one.
    assert (out.size, out.scale) == ((400, 800), 1.0)


def test_an_oversize_upload_is_refused_before_it_is_decoded() -> None:
    with pytest.raises(PhotoError, match="larger than"):
        prepare(b"x" * 2001, max_bytes=2000, max_px=2048)


def test_an_empty_upload_is_refused() -> None:
    with pytest.raises(PhotoError, match="empty"):
        prepare(b"", max_bytes=15_000_000, max_px=2048)


def test_something_that_is_not_an_image_is_refused() -> None:
    with pytest.raises(PhotoError, match="not an image"):
        prepare(b'{"name": "you posted the stats file"}', max_bytes=15_000_000, max_px=2048)


def test_a_truncated_jpeg_is_refused() -> None:
    raw = jpeg((800, 600))
    with pytest.raises(PhotoError):
        prepare(raw[: len(raw) // 3], max_bytes=15_000_000, max_px=2048)


def test_a_png_with_transparency_becomes_rgb() -> None:
    buffer = io.BytesIO()
    Image.new("RGBA", (400, 300), (10, 20, 30, 128)).save(buffer, "PNG")
    out = prepare(buffer.getvalue(), max_bytes=15_000_000, max_px=2048)
    with Image.open(io.BytesIO(out.data)) as image:
        assert image.mode == "RGB"
