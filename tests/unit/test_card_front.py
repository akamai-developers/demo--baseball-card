"""The front of the card: the picture, the frame round it, and the name plate.

compose.py makes the picture at the size card/__init__.py names, and these
check that the page shows it at exactly that box, that the plate sits under
it, and that every word on the plate fits. Each style is a template of its
own, so each one is checked.
"""

from __future__ import annotations

import io
import re

import numpy as np
import pytest
from PIL import Image

from card import BRAND_DIR, CARD_H, CARD_W, FRAME, PICTURE_H, PICTURE_W, PLATE_H, WINDOW_W
from card import render as render_module
from card.render import NO_POSITION, fixture_stats, front_html, get_style
from common.schemas import STYLES, Stats

LONG_NAME = "Bartholomew Wolfeschlegelsteinhausen"
# The longest name Stats takes, in the widest capital there is.
WIDEST_NAME = "W" * 60
NAME_PX = 112


@pytest.fixture(scope="module")
def page():
    """One Chromium page shared by every test in this module."""
    playwright_api = pytest.importorskip("playwright.sync_api")
    with playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=list(render_module.CHROMIUM_ARGS))
        yield browser.new_page(viewport={"width": CARD_W, "height": CARD_H}, device_scale_factor=1)
        browser.close()


def show(page, html: str):
    page.set_content(html, wait_until="load")
    page.wait_for_function("window.__cardReady === 1", timeout=30_000)
    return page


def a_front(style: str, colour=(255, 0, 0), **overrides) -> str:
    """The fixture player's front on a flat picture of one colour."""
    stats = fixture_stats().model_copy(update=overrides)
    return front_html(stats, style, Image.new("RGB", (PICTURE_W, PICTURE_H), colour))


def shot(page) -> np.ndarray:
    return np.asarray(Image.open(io.BytesIO(page.screenshot(type="png"))).convert("RGB"), int)


def box(page, selector: str) -> dict:
    """An element's box on the card."""
    return page.evaluate(
        """(selector) => {
            const box = document.querySelector(selector).getBoundingClientRect();
            return {left: box.left, top: box.top, width: box.width, height: box.height};
        }""",
        selector,
    )


def name_size(page) -> dict:
    return page.evaluate(
        "() => { const n = document.querySelector('.name');"
        " return {fits: n.scrollWidth <= n.clientWidth + 1,"
        " size: parseFloat(getComputedStyle(n).fontSize)}; }"
    )


def hex_rgb(colour: str) -> tuple[int, int, int]:
    return tuple(int(colour[i : i + 2], 16) for i in (1, 3, 5))


# ---------------------------------------------------------- the picture


@pytest.mark.parametrize("style", STYLES)
def test_the_picture_fills_the_window_above_the_plate(page, style):
    """What compose places is what the card shows: the picture at its own
    size, in the window's top left corner, and the plate under it."""
    show(page, a_front(style))
    assert box(page, ".hero") == {
        "left": FRAME,
        "top": FRAME,
        "width": PICTURE_W,
        "height": PICTURE_H,
    }
    assert box(page, ".plate") == {
        "left": FRAME,
        "top": FRAME + PICTURE_H,
        "width": WINDOW_W,
        "height": PLATE_H,
    }


@pytest.mark.parametrize("style", STYLES)
def test_the_frame_and_the_plate_are_drawn_round_the_picture(page, style):
    """A red picture: red inside its box, but for the window's rounded top
    corners, and the frame's colour all round it and on the plate."""
    show(page, a_front(style, name="​", position="ZZ", number=None))
    pixels = shot(page)
    red = (pixels == (255, 0, 0)).all(axis=2)
    frame = (pixels == hex_rgb(get_style(style)["palette"]["--frame"])).all(axis=2)

    inside = red[FRAME + 40 : FRAME + PICTURE_H - 1, FRAME + 40 : FRAME + PICTURE_W - 40]
    assert inside.all()
    assert red.sum() == pytest.approx(PICTURE_W * PICTURE_H, rel=0.001)
    assert not red[FRAME + PICTURE_H :].any(), "the picture shows under the plate"
    # The frame's colour: the border outside the hairline, and the plate below
    # its top rule, where the position ZZ leaves a few letters.
    assert frame[:20].all() and frame[:, :20].all() and frame[-20:].all()
    plate = frame[FRAME + PICTURE_H + 10 : CARD_H - FRAME, FRAME : FRAME + WINDOW_W]
    assert plate.mean() > 0.97


# ------------------------------------------------------------ the name


@pytest.mark.parametrize("style", STYLES)
def test_a_name_that_fits_is_left_at_full_size(page, style):
    show(page, a_front(style, name="Sam Rivera"))
    assert name_size(page) == {"fits": True, "size": NAME_PX}


@pytest.mark.parametrize("style", STYLES)
def test_a_long_name_shrinks_instead_of_running_off_the_plate(page, style):
    show(page, a_front(style, name=LONG_NAME))
    measured = name_size(page)
    assert measured["fits"], "the name overflows its box"
    assert 40 < measured["size"] < NAME_PX


@pytest.mark.parametrize("style", STYLES)
def test_the_longest_name_the_api_takes_fits_on_the_plate(page, style):
    show(page, a_front(style, name=WIDEST_NAME))
    measured = name_size(page)
    assert measured["fits"], "the name overflows its box"
    assert measured["size"] >= 16
    name, plate = box(page, ".name"), box(page, ".plate")
    assert name["left"] + name["width"] <= plate["left"] + plate["width"]


@pytest.mark.parametrize(
    ("position", "line"),
    [
        (None, NO_POSITION),
        ("SS", "Shortstop"),
        ("CF", "Center Field"),
        ("SP", "Starting Pitcher"),
        ("UTIL", "Utility"),
        ("XY", "XY"),
    ],
)
@pytest.mark.parametrize("style", STYLES)
def test_the_line_under_the_name_is_the_position_spelled_out(style, position, line):
    """With no position the line says Player."""
    html = front_html(
        fixture_stats().model_copy(update={"position": position}), style, Image.new("RGB", (8, 8))
    )
    found = re.search(r'<div class="role"[^>]*>(.*?)</div>', html, re.S)
    assert found.group(1) == line


@pytest.mark.parametrize("style", STYLES)
def test_the_words_sit_inside_the_plate(page, style):
    show(page, a_front(style))
    plate = box(page, ".plate")
    for selector in (".number", ".name", ".role"):
        part = box(page, selector)
        assert part["top"] >= plate["top"] + 10, selector
        assert part["top"] + part["height"] <= plate["top"] + plate["height"], selector
        assert part["left"] + part["width"] <= plate["left"] + plate["width"], selector


@pytest.mark.parametrize("style", STYLES)
def test_a_card_with_no_number_starts_the_name_at_the_plates_edge(page, style):
    show(page, a_front(style, number=None))
    assert page.locator(".number").count() == 0
    assert box(page, ".name")["left"] == FRAME + 64
    show(page, a_front(style, number=7))
    assert page.text_content(".number") == "7"
    assert box(page, ".name")["left"] > box(page, ".number")["left"]


def test_a_name_is_escaped():
    html = front_html(Stats(name="<b>Sam</b> & co"), "akamai", Image.new("RGB", (8, 8)))
    assert "&lt;b&gt;Sam&lt;/b&gt; &amp; co" in html
    assert "<b>Sam" not in html


# ------------------------------------------------------------ the logo


def test_only_the_akamai_front_carries_the_logo(page):
    shown = {}
    for style in STYLES:
        show(page, a_front(style))
        shown[style] = page.locator(".logo").count()
    assert shown == {"akamai": 1, "classic": 0}


def test_the_logo_is_white_on_the_navy_plate_in_its_own_proportions(page):
    """The brand guide's rules: no recolouring, proportions as provided."""
    show(page, a_front("akamai"))
    logo = box(page, ".logo")
    view = re.search(r'viewBox="([^"]+)"', (BRAND_DIR / "akamai-logo-white.svg").read_text())
    _, _, width, height = (float(number) for number in view.group(1).split())
    assert logo["width"] / logo["height"] == pytest.approx(width / height, rel=0.01)

    pixels = shot(page)
    left, top = round(logo["left"]), round(logo["top"])
    inked = pixels[top : top + round(logo["height"]), left : left + round(logo["width"])]
    white = (inked >= 245).all(axis=2)
    navy = (inked == hex_rgb(get_style("akamai")["palette"]["--frame"])).all(axis=2)
    assert white.mean() > 0.15 and navy.mean() > 0.3
    assert (white | navy).mean() > 0.8, "a colour other than white and navy in the logo's box"
