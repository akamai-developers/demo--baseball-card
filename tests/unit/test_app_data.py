"""What web/app/state.js copies, held to the files it copies.

The page knows a few things before it asks the api anything: the two styles,
the six swing numbers with their ranges, the six steps and the sample player.
Each is a copy of something elsewhere in the repo, and a copy can drift. This
reads state.js and compares.

It also holds the page's logo and font to the card's, and the page to its
size: under 5,000 lines of JavaScript, CSS and HTML.
"""

from __future__ import annotations

import json
import re

import pytest
from PIL import Image

from api.pipeline import STEPS
from card import BRAND_DIR, FONT_DIR
from card.render import load_styles
from common.schemas import METRICS, STYLES
from tests.conftest import REPO_ROOT

WEB_APP = REPO_ROOT / "web" / "app"
STATE = (WEB_APP / "state.js").read_text(encoding="utf-8")


def literal(name: str) -> str:
    """The bracketed value of `export const NAME = ...;` in state.js. No string in
    those lists holds a bracket, so counting them finds the end."""
    start = STATE.index(f"export const {name} = ") + len(f"export const {name} = ")
    depth = 0
    for index in range(start, len(STATE)):
        depth += {"[": 1, "{": 1, "]": -1, "}": -1}.get(STATE[index], 0)
        if depth == 0:
            return STATE[start : index + 1]
    raise AssertionError(f"{name} is not closed in state.js")


def test_the_styles_are_the_cards_styles() -> None:
    found = re.findall(r'\{ name: "([^"]+)", label: "([^"]+)", look: "[^"]+" \}', literal("STYLES"))
    styles = load_styles()
    assert [name for name, _ in found] == list(styles) == list(STYLES)
    assert [label for _, label in found] == [style["label"] for style in styles.values()]


@pytest.mark.parametrize("name", STYLES)
def test_every_style_has_its_tile(name: str) -> None:
    """The picker shows each style's front of the test card. CONTRIBUTING.md
    says how they are made."""
    with Image.open(WEB_APP / "styles" / f"{name}.jpg") as tile:
        assert tile.format == "JPEG"
        assert tile.size == (192, 268)


METRIC = re.compile(
    r'\{ key: "(\w+)", label: "([^"]+)", unit: "(\w+)", min: (-?[\d.]+), max: (-?[\d.]+), '
    r"step: ([\d.]+), value: (-?[\d.]+) \}"
)


def test_the_metrics_are_the_ones_the_api_takes() -> None:
    rows = METRIC.findall(literal("METRICS"))
    assert [row[0] for row in rows] == list(METRICS)
    for key, label, unit, low, high, step, value in rows:
        metric = METRICS[key]
        assert label.lower() == metric.label.lower(), key
        assert unit == metric.unit, key
        assert (float(low), float(high)) == (metric.low, metric.high), key
        assert metric.low <= float(value) <= metric.high, key
        # A whole number on the card moves the slider by whole numbers.
        assert (float(step) >= 1) == (metric.decimals == 0), key


def test_the_steps_are_the_pipelines() -> None:
    assert re.findall(r'"(\w+)"', literal("STEPS")) == list(STEPS)


def test_the_sample_player_is_examples_stats_json() -> None:
    as_json = re.sub(r"(\w+):", r'"\1":', literal("SAMPLE"))
    as_json = re.sub(r",(\s*[}\]])", r"\1", as_json)
    sample = json.loads(as_json)
    assert sample == json.loads((REPO_ROOT / "examples" / "stats.json").read_text())


def test_every_position_fits_the_api() -> None:
    positions = re.findall(r'"(\w+)"', literal("POSITIONS"))
    assert positions and all(len(position) <= 4 for position in positions)


@pytest.mark.parametrize(
    ("copy", "original"),
    [
        (WEB_APP / "brand" / "akamai-logo-white.svg", BRAND_DIR / "akamai-logo-white.svg"),
        (WEB_APP / "fonts" / "InstrumentSans.ttf", FONT_DIR / "InstrumentSans.ttf"),
        (WEB_APP / "fonts" / "OFL-InstrumentSans.txt", FONT_DIR / "OFL-InstrumentSans.txt"),
    ],
    ids=lambda path: path.name,
)
def test_the_page_uses_the_cards_logo_and_font(copy, original) -> None:
    """The same bytes, so the page and the card cannot drift apart. The font
    goes with its licence."""
    assert copy.read_bytes() == original.read_bytes()


def test_the_page_is_under_5000_lines() -> None:
    files = [path for suffix in ("*.js", "*.css", "*.html") for path in WEB_APP.rglob(suffix)]
    lines = sum(len(path.read_text(encoding="utf-8").splitlines()) for path in files)
    assert lines < 5000, f"web/app is {lines} lines"
