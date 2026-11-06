"""The art request the api builds, and the background it uses when nothing answers."""

from __future__ import annotations

import pytest

from api.art import DRAWN, art_request, fallback, seed_for
from card.backgrounds import ART_H, ART_W, NO_TEXT, prompt_for, recipe, styles
from card.render import load_styles
from common.schemas import STYLES


def test_the_recipes_cover_every_style_the_api_accepts() -> None:
    """The art service paints per style in styles.json. A style the api
    accepts but the recipes miss would never match a painted background."""
    assert styles() == STYLES == tuple(load_styles())


@pytest.mark.parametrize("style", STYLES)
def test_every_style_builds_a_prompt_from_its_own(style: str) -> None:
    assert load_styles()[style]["art_prompt"] in prompt_for(style)


def test_the_prompt_always_forbids_text_and_people() -> None:
    """The card's words are laid out in HTML. An image model that writes gets in the way."""
    for style in STYLES:
        assert prompt_for(style).endswith(NO_TEXT)


@pytest.mark.parametrize("style", STYLES)
def test_every_card_in_a_style_asks_for_the_same_picture(style: str) -> None:
    """The pool is per style. A card whose prompt said something about its
    player would never match a background painted ahead of it."""
    for card_id in ("abc", "def"):
        request = art_request(card_id, style)
        assert {key: request[key] for key in recipe(style)} == recipe(style)


def test_a_card_takes_from_the_pool_when_one_is_ready() -> None:
    """fresh is false: the pool first, and a painting with this card's seed when
    the style has none ready."""
    assert art_request("abc", "akamai")["fresh"] is False


def test_the_same_card_always_gets_the_same_seed() -> None:
    assert seed_for("abc", "akamai") == seed_for("abc", "akamai")


def test_a_different_card_gets_a_different_seed() -> None:
    assert seed_for("abc", "akamai") != seed_for("def", "akamai")


def test_the_style_changes_the_seed() -> None:
    assert seed_for("abc", "akamai") != seed_for("abc", "classic")


def test_the_request_is_the_size_flux_was_measured_at() -> None:
    """1024x1408 is what FLUX.2 klein was measured at on a 20 GB card."""
    request = art_request("abc", "classic")
    assert (request["width"], request["height"]) == (ART_W, ART_H) == (1024, 1408)
    assert request["steps"] == 4
    assert request["style"] == "classic"
    assert 0 <= request["seed"] < 2**31


@pytest.mark.parametrize("style", STYLES)
def test_the_fallback_is_a_drawn_stadium_at_the_same_size(style: str) -> None:
    assert DRAWN == "procedural"
    assert fallback(style, 7).size == (ART_W, ART_H)


def test_the_fallback_is_the_same_for_the_same_seed() -> None:
    assert fallback("akamai", 7).tobytes() == fallback("akamai", 7).tobytes()
