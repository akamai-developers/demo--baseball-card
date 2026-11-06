"""What a card's background is painted from. One recipe per style.

The api asks the art service for these, and the art service paints them ahead
of time into its pool, so the two have to agree to the character. Both import
this file.

A recipe is the style's `art_prompt` from styles.json, then the same words
every time: no text, no letters, no logos, no people. The card's words are laid
out in HTML. An image model that writes gets in the way.

Nothing about the player is in it, not even the position. A pool per style
only works if every card in a style asks for the same picture.
"""

from __future__ import annotations

import json
from functools import lru_cache

from card import STYLES_PATH

# FLUX.2 klein at 1024x1408. Compose scales it to cover the picture and crops
# what is left over, top and bottom.
ART_W = 1024
ART_H = 1408
# klein is distilled for four steps. The model card says so.
ART_STEPS = 4

NO_TEXT = "no text, no letters, no logos, no people, empty stadium"


@lru_cache(maxsize=1)
def _presets() -> dict[str, dict]:
    return json.loads(STYLES_PATH.read_text())


def styles() -> tuple[str, ...]:
    """Every style name, in styles.json order."""
    return tuple(_presets())


def prompt_for(style: str) -> str:
    """The style's own prompt, then the prohibitions. An unknown style gets the first."""
    presets = _presets()
    preset = presets.get(style) or next(iter(presets.values()))
    return f"{preset['art_prompt']}, {NO_TEXT}"


def recipe(style: str) -> dict:
    """The body of POST /art/generate for a style, less the seed.

    `style` is not part of what the model sees. It is there so the art service
    knows which pool to take from, and so the mock can pick a palette without
    reading English.
    """
    return {
        "prompt": prompt_for(style),
        "style": style,
        "width": ART_W,
        "height": ART_H,
        "steps": ART_STEPS,
    }


__all__ = ["ART_H", "ART_STEPS", "ART_W", "NO_TEXT", "prompt_for", "recipe", "styles"]
