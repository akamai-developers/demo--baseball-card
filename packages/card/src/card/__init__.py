"""The card renderer.

Two steps, in this order:

  compose.py  Pillow puts the cutout over the art, adds a ground shadow that
              OpenCV blurs, and returns one flat picture. placement.py says
              where the person goes in it.
  render.py   Chromium renders the style's front template and back.html at
              1500x2100, both at once, and writes the PNGs. Every word on the
              card comes from the HTML, never from the image model.

Sizes are fixed. 1500x2100 px is 2.5 x 3.5 inches at 600 dpi, the size of a real
baseball card.

    +--------------------------- card 1500 x 2100 ---+
    |  frame 44                                       |
    |  +------------------ window 1412 x 2012 ----+   |
    |  |                                          |   |
    |  |          picture 1412 x 1692             |   |
    |  |      the art, the shadow, the person     |   |
    |  |                                          |   |
    |  +------------------------------------------+   |
    |  |  name plate 320: number, name, position  |   |
    |  +------------------------------------------+   |
    +-------------------------------------------------+

render.py hands these numbers to card.css as custom properties, so the page
and the compositor cannot disagree about where the picture ends.
"""

from __future__ import annotations

from pathlib import Path

CARD_W = 1500
CARD_H = 2100

# The border round the window, on both sides.
FRAME = 44
WINDOW_W = CARD_W - 2 * FRAME  # 1412
WINDOW_H = CARD_H - 2 * FRAME  # 2012

# The name plate across the bottom of the window on the front, its top rule
# included. The picture is the window above it.
PLATE_H = 320
PICTURE_W = WINDOW_W  # 1412
PICTURE_H = WINDOW_H - PLATE_H  # 1692

PREVIEW_W = 500

CARD_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = CARD_DIR / "templates"
FONT_DIR = CARD_DIR / "fonts"
BRAND_DIR = CARD_DIR / "brand"
STYLES_PATH = CARD_DIR / "styles.json"
CSS_PATH = CARD_DIR / "card.css"

__all__ = [
    "BRAND_DIR",
    "CARD_DIR",
    "CARD_H",
    "CARD_W",
    "CSS_PATH",
    "FONT_DIR",
    "FRAME",
    "PICTURE_H",
    "PICTURE_W",
    "PLATE_H",
    "PREVIEW_W",
    "STYLES_PATH",
    "TEMPLATE_DIR",
    "WINDOW_H",
    "WINDOW_W",
]
