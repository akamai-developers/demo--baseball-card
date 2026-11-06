"""What the api asks the art service for, and what it uses when the answer does not come.

The request is the style's recipe from card/backgrounds.py: the style's prompt
and the prohibitions, at 1024x1408, four steps. Every card in a style asks for
the same picture, which is what lets the art service paint it before the card
is posted, into its pool. `"fresh": false` takes one from there when one is
ready, and the answer's X-Art-Source header says `pool`. When none is, the
service paints one on the spot with the seed this file picks for the card, and
the header says `fresh`.

When the art service errors or runs past ART_TIMEOUT_S, the card gets a
stadium drawn with numpy instead, card.procedural.stadium, and the ledger says
so. A card always has a background.
"""

from __future__ import annotations

import zlib

from PIL import Image

from card.backgrounds import ART_H, ART_W, recipe
from card.procedural import stadium

# What the ledger's model column says for a background the art service did not
# paint.
DRAWN = "procedural"


def seed_for(card_id: str, style: str) -> int:
    """A stable seed. The same card in the same style always gets the same fresh
    background."""
    return zlib.crc32(f"{card_id}|{style}".encode()) & 0x7FFFFFFF


def art_request(card_id: str, style: str) -> dict:
    """The body of POST /art/generate for this card.

    `fresh` is false: a background from the pool when one is ready, and a new one
    painted with this card's seed when none is.
    """
    return {**recipe(style), "seed": seed_for(card_id, style), "fresh": False}


def fallback(style: str, seed: int) -> Image.Image:
    """The drawn stadium for the style. Never raises: this is the path a card
    takes when something has already gone wrong."""
    return stadium(style, ART_W, ART_H, seed=seed)


__all__ = ["DRAWN", "art_request", "fallback", "seed_for"]
