"""The card's static assets and the small pure functions that feed the templates."""

from __future__ import annotations

import json
import re

import pytest

from card import BRAND_DIR, CSS_PATH, FONT_DIR, STYLES_PATH, TEMPLATE_DIR
from card.backgrounds import ART_H, ART_STEPS, ART_W, NO_TEXT, prompt_for, recipe, styles
from card.render import (
    FONT_FILE,
    MADE_ON_CPU,
    MADE_ON_GPU,
    MADE_ON_MOCK,
    MADE_ON_SOME,
    _css_vars,
    bio_items,
    get_style,
    load_styles,
    made_line,
    metric_rows,
    position_line,
)
from common.schemas import STYLES, Stats, Step

# The custom properties render.py sets from card/__init__.py for every style.
SIZES = {
    "--card-w",
    "--card-h",
    "--frame-w",
    "--window-w",
    "--window-h",
    "--picture-h",
    "--plate-h",
}


def test_styles_json_covers_exactly_the_named_styles_in_order():
    assert tuple(load_styles()) == STYLES
    assert list(json.loads(STYLES_PATH.read_text())) == list(STYLES)


@pytest.mark.parametrize("name", STYLES)
def test_a_style_is_a_label_a_prompt_and_colours(name):
    """Nothing else: the font, the sizes and the shadow are the card's, the
    same for every style, and there is no light painted round the person."""
    style = load_styles()[name]
    assert set(style) == {"label", "art_prompt", "palette"}
    assert style["label"] and style["art_prompt"]
    assert all(re.fullmatch(r"#[0-9a-f]{6}", colour) for colour in style["palette"].values())


@pytest.mark.parametrize("name", STYLES)
def test_the_palette_is_exactly_what_the_stylesheet_reads(name):
    """Every custom property card.css reads is defined, or a colour silently
    falls back to the browser default. And every colour defined is read."""
    read = set(re.findall(r"var\((--[\w-]+)\)", CSS_PATH.read_text()))
    assert set(load_styles()[name]["palette"]) == read - SIZES


def test_the_sizes_the_stylesheet_reads_come_from_the_card_package():
    defined = set(re.findall(r"(--[\w-]+):", _css_vars(get_style("akamai"))))
    assert SIZES.issubset(defined)


@pytest.mark.parametrize("name", STYLES)
def test_every_style_has_its_front_template_and_nothing_else_does(name):
    assert (TEMPLATE_DIR / f"front-{name}.html").is_file()
    assert sorted(path.name for path in TEMPLATE_DIR.iterdir()) == sorted(
        ["base.html", "back.html", *(f"front-{style}.html" for style in STYLES)]
    )


def test_an_unknown_style_falls_back_instead_of_raising():
    assert get_style("no-such-style") == get_style("akamai")
    assert get_style(None)["label"] == "Akamai"


def test_the_font_ships_with_its_licence_and_alone():
    assert (FONT_DIR / FONT_FILE).stat().st_size > 50_000
    assert "SIL Open Font License" in (FONT_DIR / "OFL-InstrumentSans.txt").read_text()
    assert sorted(path.name for path in FONT_DIR.iterdir()) == [
        "InstrumentSans.ttf",
        "OFL-InstrumentSans.txt",
    ]


def test_the_logo_is_the_all_white_mark_and_its_note_says_where_it_is_from():
    """The brand guide says no recolouring: the one fill is white."""
    assert sorted(path.name for path in BRAND_DIR.iterdir()) == [
        "README.md",
        "akamai-logo-white.svg",
    ]
    svg = (BRAND_DIR / "akamai-logo-white.svg").read_text()
    assert set(re.findall(r"fill:\s*(#[0-9a-fA-F]+)", svg)) == {"#fff"}
    note = (BRAND_DIR / "README.md").read_text()
    assert "akamai.com/newsroom/media-resources" in note
    assert "trademark" in note


# ------------------------------------------------- what feeds the templates


def test_metric_rows_labels_each_metric_and_keeps_input_order():
    rows = metric_rows({"exit_velo_mph": 88.5, "time_to_contact_ms": 155})
    assert [row["label"] for row in rows] == ["Exit Velocity", "Time to Contact"]
    assert [row["value"] for row in rows] == ["88.5", "155"]
    assert [row["unit"] for row in rows] == ["mph", "ms"]


def test_metric_rows_of_nothing_is_no_rows():
    assert metric_rows({}) == []


def test_bio_items_drops_everything_that_was_not_supplied():
    assert bio_items(Stats(name="Nobody")) == []


def test_bio_items_formats_height_with_real_prime_marks():
    stats = Stats(
        name="Sam Rivera", bats="R", throws="L", height_in=75, weight_lb=190, hometown="Chicago, IL"
    )
    assert bio_items(stats) == ["Bats R", "Throws L", "6′ 3″", "190 lb", "Chicago, IL"]


def test_a_switch_hitter_says_switch():
    assert bio_items(Stats(name="Sam Rivera", bats="S")) == ["Switch"]


def test_the_position_is_spelled_out_or_left_as_typed_or_player():
    assert position_line(Stats(name="A", position="1B")) == "First Base"
    assert position_line(Stats(name="A", position="Q")) == "Q"
    assert position_line(Stats(name="A")) == "Player"


# ------------------------------------------------- what the card may claim


def _step(name: str, ran_on: str, status: str = "done") -> Step:
    return Step(step=name, status=status, model=f"mock/{name}", license="n/a", ran_on=ran_on, ms=1)


def test_a_run_with_every_model_on_a_gpu_may_claim_the_gpus():
    steps = [
        _step("cutout", "akamai-gpu0"),
        _step("art", "akamai-gpu0"),
        # The comps lookup is numpy on the host. It is not a model step, so it
        # does not get a say in whether the card may name the GPUs.
        _step("comps", "akamai-cpu"),
        _step("text", "akamai-gpu1"),
        _step("compose", "akamai-cpu"),
        _step("render", "akamai-cpu"),
    ]
    assert made_line(steps) == MADE_ON_GPU


def test_a_mock_run_says_no_gpu_was_used():
    steps = [_step("cutout", "mock"), _step("art", "mock"), _step("text", "mock")]
    assert made_line(steps) == MADE_ON_MOCK


def test_the_laptop_stacks_mock_gpu_labels_are_still_a_mock_run():
    """The laptop labels each mock by the GPU it stands in for, so the app
    shows the node's layout. No GPU touched any of it, and the card says so."""
    steps = [_step("cutout", "mock-gpu0"), _step("art", "mock-gpu0"), _step("text", "mock-gpu0")]
    assert made_line(steps) == MADE_ON_MOCK


def test_a_run_that_fell_back_to_the_cpu_claims_neither():
    steps = [
        _step("cutout", "akamai-gpu0"),
        _step("art", "akamai-cpu"),
        _step("text", "akamai-gpu1"),
    ]
    assert made_line(steps) == MADE_ON_SOME


def test_a_real_model_on_a_cpu_is_not_a_mock_run():
    """BiRefNet really ran on a laptop. No GPU did."""
    steps = [_step("cutout", "akamai-cpu"), _step("art", "mock"), _step("text", "mock")]
    assert made_line(steps) == MADE_ON_CPU


def test_a_card_with_no_finished_model_step_claims_nothing():
    assert made_line([]) == MADE_ON_MOCK
    assert made_line([_step("cutout", "akamai-gpu0", status="failed")]) == MADE_ON_MOCK


# ------------------------------------------------- what the art is painted from


@pytest.mark.parametrize("style", STYLES)
def test_a_recipe_is_the_styles_prompt_and_the_same_prohibitions(style):
    asked = recipe(style)
    assert asked == {
        "prompt": f"{get_style(style)['art_prompt']}, {NO_TEXT}",
        "style": style,
        "width": ART_W,
        "height": ART_H,
        "steps": ART_STEPS,
    }
    assert (ART_W, ART_H, ART_STEPS) == (1024, 1408, 4)
    assert "no text" in asked["prompt"] and "no people" in asked["prompt"]


def test_the_night_stadium_is_the_akamai_style_and_the_day_park_the_classic():
    assert prompt_for("akamai").startswith("an empty baseball stadium at night")
    assert "summer afternoon" in prompt_for("classic")
    assert prompt_for("no-such-style") == prompt_for("akamai")
    assert styles() == STYLES
