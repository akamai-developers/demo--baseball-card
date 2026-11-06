"""What Chromium actually produces.

These launch a real browser. `make test` installs Playwright's Chromium first,
they take a few seconds, and they are the only way to check the things that
matter here: the PNG is exactly 1500x2100, a section with no data leaves no
empty rows, the page never touches the network, and two runs give the same
bytes. The front's layout has tests of its own, in test_card_front.py.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import io
import re
import threading
import time
from pathlib import Path

import pytest
from PIL import Image

from card import BRAND_DIR, CARD_H, CARD_W
from card import render as render_module
from card.compose import compose_hero
from card.procedural import stadium
from card.render import (
    FIXTURE_COMPS,
    FIXTURE_CUTOUT,
    FIXTURE_FACE,
    FIXTURE_STEPS,
    FIXTURE_TEXT,
    MADE_ON_GPU,
    MADE_ON_MOCK,
    Chromium,
    Folder,
    back_html,
    draw_card,
    fixture_stats,
    front_html,
    keep_units,
    render_card,
    render_fixture,
)
from common.schemas import METRICS, STYLES, CardText, Stats, Step

LONG_NAME = "Bartholomew Wolfeschlegelsteinhausen"
HTTP = re.compile(r"^https?://", re.IGNORECASE)

# What the ledger looks like after a card on the GPU node, with the longest
# names a row can carry: the model ids, and the pool beside the art's.
GPU_STEPS = [
    Step(
        step="cutout",
        status="done",
        model="ZhengPeng7/BiRefNet",
        license="MIT",
        ran_on="akamai-gpu0",
        ms=412,
    ),
    Step(
        step="art",
        status="done",
        model="black-forest-labs/FLUX.2-klein-4B",
        license="Apache-2.0",
        ran_on="akamai-gpu0",
        ms=24,
        source="pool",
    ),
    Step(
        step="comps",
        status="done",
        model="nearest neighbour",
        license="n/a",
        ran_on="akamai-cpu",
        ms=6,
    ),
    Step(
        step="text",
        status="done",
        model="google/gemma-4-12B-it-qat-w4a16-ct",
        license="Apache-2.0",
        ran_on="akamai-gpu0",
        ms=1640,
    ),
]


@pytest.fixture(scope="module")
def browser_page():
    """One Chromium page shared by every test in this module."""
    playwright_api = pytest.importorskip("playwright.sync_api")
    with playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=list(render_module.CHROMIUM_ARGS))
        page = browser.new_page(viewport={"width": CARD_W, "height": CARD_H}, device_scale_factor=1)
        yield page
        browser.close()


def show(page, html: str):
    page.set_content(html, wait_until="load")
    page.wait_for_function("window.__cardReady === 1", timeout=30_000)
    return page


def a_front(style: str = "akamai", **overrides) -> str:
    stats = fixture_stats().model_copy(update=overrides)
    return front_html(stats, style, compose_hero(stadium(style, 256, 352, seed=1), None))


def a_back(stats: Stats, text: CardText | None = None, comps=(), steps=(), style="akamai") -> str:
    return back_html(stats, style, text or CardText(), comps, steps)


def _lines(page, selector: str) -> list[int]:
    """How many lines of text each element matching `selector` is set on.

    Text boxes on one line overlap from top to bottom, whatever their font size.
    A box that starts below the bottom of the line above it starts a new line.
    """
    return page.evaluate(
        """(selector) => [...document.querySelectorAll(selector)].map((el) => {
            const range = document.createRange();
            range.selectNodeContents(el);
            const boxes = [...range.getClientRects()].sort((a, b) => a.top - b.top);
            let lines = 0;
            let bottom = -Infinity;
            for (const box of boxes) {
                if (box.top >= bottom - 1) lines += 1;
                bottom = lines && box.top >= bottom - 1 ? box.bottom : Math.max(bottom, box.bottom);
            }
            return lines;
        })""",
        selector,
    )


# ------------------------------------------------------------------ the back


def test_a_back_with_no_metrics_has_no_metrics_table(browser_page):
    show(browser_page, a_back(Stats(name="Sam Rivera")))
    counts = browser_page.evaluate(
        "() => ({metrics: document.querySelectorAll('.metrics tr').length,"
        " sections: document.querySelectorAll('.cols').length,"
        " comps: document.querySelectorAll('.comp').length,"
        " scouting: document.querySelectorAll('.scouting').length,"
        " bio: document.querySelectorAll('.bio').length})"
    )
    assert counts == {"metrics": 0, "sections": 0, "comps": 0, "scouting": 0, "bio": 0}


def test_the_back_header_is_the_name_and_the_number() -> None:
    back = a_back(Stats(name="Sam Rivera", number=23), FIXTURE_TEXT)
    header = re.search(r"<header>(.*?)</header>", back, re.S).group(1)
    assert re.findall(r'<div class="(\w+)"', header) == ["who", "name", "number"]
    assert '<div class="number">' not in a_back(Stats(name="Sam Rivera"), FIXTURE_TEXT)


def test_a_back_renders_exactly_the_metrics_it_was_given(browser_page):
    stats = Stats(name="Sam Rivera", metrics={"bat_speed_mph": 70.0, "exit_velo_mph": 90.0})
    show(browser_page, a_back(stats, FIXTURE_TEXT, FIXTURE_COMPS, GPU_STEPS))
    counts = browser_page.evaluate(
        "() => ({metrics: document.querySelectorAll('.metrics tr').length,"
        " comps: document.querySelectorAll('.comp').length,"
        " steps: document.querySelectorAll('.provenance tbody tr').length})"
    )
    assert counts == {"metrics": 2, "comps": 3, "steps": len(GPU_STEPS)}


def test_the_columns_are_headed_swing_metrics_and_swings_like() -> None:
    back = a_back(fixture_stats(), FIXTURE_TEXT, FIXTURE_COMPS)
    assert re.findall(r"<h2>(.*?)</h2>", back) == [
        "Swing metrics",
        "Swings like",
        "Scouting report",
    ]


def test_the_numbers_in_the_blurb_read_from_across_a_room(browser_page):
    """The blurb is built on numbers. Tabular digits keep the point from
    tucking under a 7, so 7.1 ft does not read as 71, and a pretty wrap keeps a
    single word off the last line."""
    show(browser_page, a_back(fixture_stats(), FIXTURE_TEXT, FIXTURE_COMPS, GPU_STEPS))
    style = browser_page.evaluate(
        "() => { const s = getComputedStyle(document.querySelector('.scouting p'));"
        " return [s.fontVariantNumeric, s.textWrapStyle]; }"
    )
    assert style == ["tabular-nums", "pretty"]


# The longest value each metric can have: the end of its range with more
# characters, as the api takes it.
LONGEST = {
    key: max((metric.low, metric.high), key=lambda end: len(f"{end:.{metric.decimals}f}"))
    for key, metric in METRICS.items()
}


def test_every_metric_value_stays_on_one_line_at_its_longest(browser_page):
    stats = Stats(name="Sam Rivera", metrics=LONGEST)
    show(browser_page, a_back(stats, FIXTURE_TEXT, FIXTURE_COMPS, GPU_STEPS))
    assert (
        browser_page.evaluate(
            "getComputedStyle(document.querySelector('.metrics td.value')).whiteSpace"
        )
        == "nowrap"
    )
    assert _lines(browser_page, ".metrics td.value") == [1] * 6


# A back Gemma wrote on a GPU node. At this width the line broke between "7.1"
# and "ft".
NODE_BLURB = (
    "Sam gets the bat to the ball in 155 ms. The 88.5 mph exit velocity follows a 7.1 ft "
    "swing. A 14.0 degree launch angle and 9.0 degree attack angle keep it low. Ground "
    "balls and singles."
)


def test_a_number_in_the_blurb_stays_on_the_line_with_its_unit(browser_page):
    text = FIXTURE_TEXT.model_copy(update={"blurb": NODE_BLURB})
    show(browser_page, a_back(fixture_stats(), text, FIXTURE_COMPS, GPU_STEPS))
    held = browser_page.evaluate(
        "() => [...document.querySelectorAll('.scouting .nb')].map((el) => el.textContent)"
    )
    assert held == ["155 ms", "88.5 mph", "7.1 ft", "14.0 degree", "9.0 degree"]
    assert _lines(browser_page, ".scouting .nb") == [1] * 5
    assert _lines(browser_page, ".scouting p") == [3], "the blurb itself still wraps"


def test_keep_units_joins_each_number_to_its_unit_and_escapes_the_rest():
    held = keep_units("A <b> 7.1 ft swing, -5 deg, 22 degrees & 155 ms. 96.3.")
    assert str(held) == (
        'A &lt;b&gt; <span class="nb">7.1 ft</span> swing, <span class="nb">-5 deg</span>, '
        '<span class="nb">22 degrees</span> &amp; <span class="nb">155 ms</span>. 96.3.'
    )
    assert str(keep_units("no numbers here")) == "no numbers here"


def test_the_provenance_strip_names_its_columns(browser_page):
    """Without these the last column is bare numbers and no unit."""
    show(browser_page, a_back(Stats(name="Sam Rivera"), FIXTURE_TEXT, (), GPU_STEPS))
    headers = browser_page.evaluate(
        "() => [...document.querySelectorAll('.provenance thead th')].map(th => th.textContent)"
    )
    assert headers == ["Step", "Model", "License", "Ran on", "ms"]


def test_the_strip_says_the_background_came_from_the_pool_and_it_fits(browser_page):
    """Next to the model, and not cut off by the column's ellipsis."""
    show(browser_page, a_back(Stats(name="Sam Rivera"), FIXTURE_TEXT, (), GPU_STEPS))
    cells = browser_page.evaluate(
        """() => [...document.querySelectorAll('.provenance tbody td.model')].map(td => ({
            text: td.textContent.trim(),
            clipped: td.scrollWidth > td.clientWidth,
        }))"""
    )
    art = cells[[step.step for step in GPU_STEPS].index("art")]
    assert art == {"text": "black-forest-labs/FLUX.2-klein-4B · pool", "clipped": False}
    assert not any(cell["clipped"] for cell in cells)


def test_a_step_that_did_not_finish_or_named_no_model_has_no_row() -> None:
    steps = [
        *GPU_STEPS,
        Step(step="compose", status="done", ran_on="akamai-cpu", ms=90),
        Step(step="render", status="failed", model="chromium", error="it broke"),
    ]
    back = a_back(Stats(name="Sam Rivera"), FIXTURE_TEXT, (), steps)
    rows = re.findall(r'<td class="step">(\w+)</td>', back)
    assert rows == ["cutout", "art", "comps", "text"]


def test_the_akamai_line_is_printed_when_every_model_step_ran_on_a_gpu(browser_page):
    show(browser_page, a_back(Stats(name="Sam Rivera"), FIXTURE_TEXT, (), GPU_STEPS))
    assert browser_page.text_content(".provenance .made").strip() == MADE_ON_GPU


def test_a_mock_run_says_so_instead_of_claiming_gpus(browser_page):
    """The fixture's ledger is the stand-ins', and the card says so."""
    show(browser_page, a_back(Stats(name="Sam Rivera"), FIXTURE_TEXT, (), FIXTURE_STEPS))
    assert browser_page.text_content(".provenance .made").strip() == MADE_ON_MOCK


@pytest.mark.parametrize("style", STYLES)
def test_the_longest_back_still_fits(browser_page, style):
    """The longest name the api takes, a long hometown, a long tagline and
    blurb, six metrics, three comps and the ledger. Nothing may run past the
    bottom of the card, and the name shrinks to fit its line."""
    stats = fixture_stats().model_copy(update={"name": "W" * 60, "hometown": "H" * 60})
    text = FIXTURE_TEXT.model_copy(
        update={
            "tagline": "Quick to the ball and quicker down the line to first",
            "blurb": " ".join(
                ["Sam gets the bat to the ball in 155 ms, a short swing at 7.1 ft."] * 4
            ),
        }
    )
    show(browser_page, a_back(stats, text, FIXTURE_COMPS, GPU_STEPS, style))
    measured = browser_page.evaluate(
        """() => {
          const box = (selector) => document.querySelector(selector).getBoundingClientRect();
          const name = document.querySelector('header .name');
          return { card: box('.window').bottom, made: box('.provenance .made').bottom,
                   fits: name.scrollWidth <= name.clientWidth + 1 };
        }"""
    )
    assert measured["made"] <= measured["card"], measured
    assert measured["fits"], "the name runs past its line"


def test_nothing_on_the_page_reaches_the_network(browser_page):
    asked_for: list[str] = []
    browser_page.on("request", lambda request: asked_for.append(request.url))
    for style in STYLES:
        show(browser_page, a_front(style))
        show(browser_page, a_back(fixture_stats(), FIXTURE_TEXT, FIXTURE_COMPS, GPU_STEPS, style))
    assert [url for url in asked_for if HTTP.match(url)] == []


def test_a_page_that_reaches_for_the_network_fails_the_card(tmp_path):
    """The route handler refuses the request, and the card is not written. The
    address is in a top-level domain that never resolves, in case it got out."""
    folder = Folder(tmp_path)
    paths = _card_paths(tmp_path / "card")
    page = (
        '<html><body><img src="http://card.invalid/pixel.png">'
        "<script>window.__cardReady = 1</script></body></html>"
    )
    sides = {"front": lambda: folder.page("front", page)}
    with pytest.raises(RuntimeError, match="tried to reach the network: http://card.invalid/"):
        render_module.CHROMIUM.run(lambda browser: draw_card(browser, sides, paths))
    assert not any(path.exists() for path in paths.values())


@pytest.mark.parametrize("style", STYLES)
def test_the_font_the_card_asks_for_is_the_font_that_renders(browser_page, style):
    show(browser_page, a_front(style))
    family = browser_page.evaluate("getComputedStyle(document.querySelector('.name')).fontFamily")
    assert family.startswith('"Instrument Sans"')
    assert browser_page.evaluate("document.fonts.check('700 112px \"Instrument Sans\"')")
    show(browser_page, a_back(fixture_stats(), FIXTURE_TEXT, FIXTURE_COMPS, GPU_STEPS, style))
    body = browser_page.evaluate("getComputedStyle(document.querySelector('.bio')).fontFamily")
    assert body.startswith('"Instrument Sans"')


# -------------------------------------------------------------------- the PNGs


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    return render_fixture(tmp_path_factory.mktemp("card"))


def test_the_renderer_writes_a_front_a_back_and_a_preview(rendered):
    assert set(rendered) == {"front", "back", "preview"}
    assert all(path.is_file() for path in rendered.values())


@pytest.mark.parametrize("side", ["front", "back"])
def test_both_sides_come_out_at_exactly_1500x2100(rendered, side):
    with Image.open(rendered[side]) as image:
        assert image.size == (CARD_W, CARD_H)
        assert image.format == "PNG"


def test_the_preview_is_a_small_jpeg_of_the_front(rendered):
    with Image.open(rendered["preview"]) as preview:
        assert preview.format == "JPEG"
        assert preview.width == 500
        assert preview.height == 700


def test_two_runs_of_the_same_card_give_the_same_bytes(tmp_path):
    first = render_fixture(tmp_path / "one")
    second = render_fixture(tmp_path / "two")
    for side in ("front", "back"):
        assert first[side].read_bytes() == second[side].read_bytes(), f"{side} is not deterministic"


@pytest.mark.parametrize("style", STYLES)
def test_every_style_renders(tmp_path, style):
    written = render_fixture(tmp_path, style, basename=style)
    with Image.open(written["front"]) as front:
        assert front.size == (CARD_W, CARD_H)


def test_the_fixture_command_says_what_it_wrote_and_refuses_a_style_it_lacks(tmp_path, capsys):
    assert render_module.main(["--fixture", "--style", "classic", "--out", str(tmp_path)]) == 0
    printed = capsys.readouterr().out.split()
    assert printed == [str(tmp_path / name) for name in render_module.file_names("card").values()]
    with pytest.raises(SystemExit):
        render_module.main(["--fixture", "--style", "summer", "--out", str(tmp_path)])
    assert "Use one of: akamai, classic" in capsys.readouterr().err


class NoBrowser:
    """Runs a card's drawing with no browser, for a test that fakes the drawing.

    On a thread of its own, as Chromium does: the sync API's loop is running
    on this one while the module's browser page is open.
    """

    def run(self, work):
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, work(None)).result()


def test_render_card_composes_the_picture_with_the_face(monkeypatch, tmp_path):
    """Compose places the person by the face box, and the front gets what it made."""
    seen: dict = {}

    def compose(art, cutout, **extra):
        seen.update(compose_extra=extra)
        return Image.new("RGB", (64, 32))

    def front(stats, style, hero, picture=None, asset=None):
        seen.update(hero=hero.size, style=style)
        return "<html></html>"

    async def draw(browser, sides, paths):
        for build in sides.values():
            build()
        for path in paths.values():
            Image.new("RGB", (CARD_W, CARD_H)).save(path)

    monkeypatch.setattr(render_module, "compose_hero", compose)
    monkeypatch.setattr(render_module, "front_html", front)
    monkeypatch.setattr(render_module, "draw_card", draw)
    render_card(
        fixture_stats(),
        style="classic",
        art=Image.new("RGB", (64, 88)),
        cutout=Image.new("RGBA", (64, 88)),
        out_dir=tmp_path,
        face=FIXTURE_FACE,
        chromium=NoBrowser(),
    )
    assert seen == {"compose_extra": {"face": FIXTURE_FACE}, "hero": (64, 32), "style": "classic"}


# ------------------------------------------------------ one browser, two pages


def _pixels(png) -> bytes:
    """A PNG's pixels, from a path or from bytes. Two encoders, one picture."""
    source = png if isinstance(png, Path) else io.BytesIO(png)
    with Image.open(source) as image:
        return image.convert("RGB").tobytes()


def _shot_inline(html: str) -> bytes:
    """The page as one string, loaded with set_content and shot with Playwright's
    own screenshot."""

    async def shoot(browser):
        page = await browser.new_page(
            viewport={"width": CARD_W, "height": CARD_H}, device_scale_factor=1
        )
        try:
            await page.set_content(html, wait_until="load")
            await page.wait_for_function("window.__cardReady === 1", timeout=30_000)
            return await page.screenshot(type="png")
        finally:
            await page.close()

    return render_module.CHROMIUM.run(shoot)


async def _contexts(browser) -> int:
    return len(browser.contexts)


def _fixture_hero(style: str) -> Image.Image:
    """The picture compose makes for the fixture in this style, as the pipeline hands it over."""
    with Image.open(FIXTURE_CUTOUT) as cutout:
        return compose_hero(
            stadium(style, 1024, 1408, seed=11), cutout.convert("RGBA"), face=FIXTURE_FACE
        )


@pytest.mark.parametrize("style", STYLES)
def test_a_page_read_from_a_folder_draws_what_the_inline_page_draws(tmp_path, style):
    """The renderer writes each side to a folder with its pictures beside it, a
    BMP picture and the logo where it is, and shoots it with Chromium's quick
    PNG encoder. Neither changes a pixel of the page as one string, shot the
    way Playwright shoots it."""
    stats = fixture_stats()
    hero = _fixture_hero(style)
    written = render_card(
        stats,
        style=style,
        art=hero,
        out_dir=tmp_path,
        text=FIXTURE_TEXT,
        comps=FIXTURE_COMPS,
        steps=FIXTURE_STEPS,
    )
    front = _shot_inline(front_html(stats, style, hero))
    back = _shot_inline(back_html(stats, style, FIXTURE_TEXT, FIXTURE_COMPS, FIXTURE_STEPS))
    assert _pixels(written["front"]) == _pixels(front)
    assert _pixels(written["back"]) == _pixels(back)


@pytest.mark.parametrize(
    ("style", "files"), [("akamai", ["akamai-logo-white.svg"]), ("classic", [])]
)
def test_a_page_gets_the_picture_and_only_the_brand_files_it_shows(style, files):
    """One picture to write, and the logo to load on the akamai front only."""
    asked: list[str] = []
    loaded: list[Path] = []

    def picture(image, name):
        asked.append(name)
        return f"{name}.png"

    def asset(path):
        loaded.append(path)
        return path.name

    page = front_html(fixture_stats(), style, Image.new("RGB", (64, 88)), picture, asset)
    assert asked == ["hero"]
    assert loaded == [BRAND_DIR / name for name in files]
    for name in files:
        assert f'src="{name}"' in page


def test_the_inline_logo_is_read_once():
    first = render_module.inline_file(BRAND_DIR / "akamai-logo-white.svg")
    assert first.startswith("data:image/svg+xml;base64,")
    assert render_module.inline_file(BRAND_DIR / "akamai-logo-white.svg") is first


def test_a_folder_keeps_a_solid_picture_as_bmp_and_a_cutout_as_png(tmp_path):
    folder = Folder(tmp_path)
    assert folder.picture(Image.new("RGB", (8, 8), (255, 0, 0)), "hero") == "hero.bmp"
    assert folder.picture(Image.new("RGBA", (8, 8), (0, 0, 0, 0)), "subject") == "subject.png"
    with Image.open(tmp_path / "hero.bmp") as bmp:
        assert (bmp.format, bmp.getpixel((3, 3))) == ("BMP", (255, 0, 0))
    with Image.open(tmp_path / "subject.png") as png:
        assert (png.format, png.mode) == ("PNG", "RGBA")
    assert folder.page("front", "<p>hi</p>") == (tmp_path / "front.html").as_uri()
    assert (tmp_path / "front.html").read_text() == "<p>hi</p>"


def _card_paths(out: Path) -> dict[str, Path]:
    out.mkdir(parents=True, exist_ok=True)
    return {"front": out / "front.png", "back": out / "back.png", "preview": out / "preview.jpg"}


def test_the_front_and_its_preview_do_not_wait_for_the_back(tmp_path):
    """The back's page is not even written until the front and its preview are
    on disk under their own names, so the api can serve the front while the
    back is still being drawn."""
    folder = Folder(tmp_path)
    paths = _card_paths(tmp_path / "card")
    seen: dict[str, bool] = {}

    def back() -> str:
        deadline = time.monotonic() + 20
        while not paths["preview"].exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        seen.update({side: path.exists() for side, path in paths.items()})
        return folder.page(
            "back", a_back(fixture_stats(), FIXTURE_TEXT, FIXTURE_COMPS, FIXTURE_STEPS)
        )

    sides = {"front": lambda: folder.page("front", a_front()), "back": back}
    render_module.CHROMIUM.run(lambda browser: draw_card(browser, sides, paths))

    assert seen == {"front": True, "back": False, "preview": True}
    assert all(path.is_file() for path in paths.values())
    with Image.open(paths["preview"]) as preview:
        assert (preview.format, preview.size) == ("JPEG", (500, 700))
    assert sorted(path.name for path in paths["front"].parent.iterdir()) == [
        "back.png",
        "front.png",
        "preview.jpg",
    ], "a .part file was left behind"


def test_the_two_sides_are_drawn_at_the_same_time(tmp_path):
    """Neither side waits for the other: each side's page is built while the
    other's is. Drawn one after the other, the first would wait here for ever."""
    folder = Folder(tmp_path)
    paths = _card_paths(tmp_path / "card")
    started = {"front": threading.Event(), "back": threading.Event()}

    def building(side: str, other: str, html):
        def build() -> str:
            started[side].set()
            if not started[other].wait(10):
                raise AssertionError(f"the {side} was built alone")
            return folder.page(side, html())

        return build

    sides = {
        "front": building("front", "back", a_front),
        "back": building(
            "back",
            "front",
            lambda: a_back(fixture_stats(), FIXTURE_TEXT, FIXTURE_COMPS, FIXTURE_STEPS),
        ),
    }
    render_module.CHROMIUM.run(lambda browser: draw_card(browser, sides, paths))
    assert all(path.is_file() for path in paths.values())


def test_render_card_writes_the_names_it_is_given(tmp_path):
    """The pipeline hands over the names the routes serve, so a file is served
    the moment it is written. Nothing else lands beside them."""
    names = {"front": "front.png", "back": "back.png", "preview": "preview.jpg"}
    written = render_card(
        fixture_stats(),
        style="akamai",
        art=Image.new("RGB", (64, 88), (30, 60, 90)),
        out_dir=tmp_path,
        names=names,
    )
    assert {side: path.name for side, path in written.items()} == names
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(names.values())


def test_a_side_that_fails_stops_the_card_and_says_why(tmp_path):
    folder = Folder(tmp_path)
    paths = _card_paths(tmp_path / "card")

    def broken() -> str:
        raise ValueError("the back would not build")

    sides = {"front": lambda: folder.page("front", a_front()), "back": broken}
    with pytest.raises(ValueError, match="would not build"):
        render_module.CHROMIUM.run(lambda browser: draw_card(browser, sides, paths))
    assert not paths["back"].exists()
    assert render_module.CHROMIUM.run(_contexts) == 0, "the card left a page open"


def test_a_card_leaves_nothing_open_in_the_browser(tmp_path):
    """The browser outlives every card. Each card's pages go with the card."""
    render_fixture(tmp_path)
    render_fixture(tmp_path, "classic", basename="classic")
    assert render_module.CHROMIUM.run(_contexts) == 0


def test_a_browser_that_died_is_started_again_for_the_next_card(tmp_path):
    chromium = Chromium()
    try:
        stats = fixture_stats()
        art = Image.new("RGB", (64, 88), (30, 60, 90))
        first = render_card(
            stats, style="akamai", art=art, out_dir=tmp_path / "one", chromium=chromium
        )
        chromium.run(lambda browser: browser.close())
        second = render_card(
            stats, style="akamai", art=art, out_dir=tmp_path / "two", chromium=chromium
        )
        assert _pixels(first["front"]) == _pixels(second["front"])
        # And after close(), the next card starts a browser and a thread again.
        chromium.close()
        third = render_card(
            stats, style="akamai", art=art, out_dir=tmp_path / "three", chromium=chromium
        )
        assert _pixels(third["front"]) == _pixels(first["front"])
    finally:
        chromium.close()
