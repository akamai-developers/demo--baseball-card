"""Turn a job into two PNGs with Chromium.

Every word on the card is laid out by the style's front template and by
back.html. The image models never write text, because they cannot spell. The
front shows the picture compose.py returns above the name plate.

A style is a front template, templates/front-<style>.html, and an entry in
styles.json: its colours and the prompt its background is painted from. The
back is the same template for every style, in its colours.

The page is built as one self-contained string: the stylesheet, the font and
the artwork are all inlined. To render a card, each side's page is written to a
scratch folder with its pictures beside it, and Chromium opens it from there.
Nothing comes over the network. A route handler blocks http, https, ws and ftp
so that stays true, and a test asserts no such request is ever made.

The front and the back render at the same time, in two pages of one Chromium
that stays up between cards. Each file is written the moment its side is done,
so the front and its preview are on disk, and served, while the back is still
being drawn.

    uv run python -m card.render --fixture
    uv run python -m card.render --fixture --all-styles
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import base64
import contextlib
import io
import json
import re
import tempfile
import threading
from collections.abc import Awaitable, Callable, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape
from PIL import Image

from card import (
    BRAND_DIR,
    CARD_H,
    CARD_W,
    CSS_PATH,
    FONT_DIR,
    FRAME,
    PICTURE_H,
    PLATE_H,
    PREVIEW_W,
    STYLES_PATH,
    TEMPLATE_DIR,
    WINDOW_H,
    WINDOW_W,
)
from card.compose import compose_hero
from card.placement import Box
from common.schemas import METRICS, CardText, Comp, Stats, Step

# The one font on the card, under the SIL Open Font License, in fonts/.
FONT_FAMILY = "Instrument Sans"
FONT_FILE = "InstrumentSans.ttf"

# What the front calls a position. cardctl takes any four characters, and one
# that is not here is printed as it was typed.
POSITIONS = {
    "P": "Pitcher",
    "SP": "Starting Pitcher",
    "RP": "Relief Pitcher",
    "C": "Catcher",
    "1B": "First Base",
    "2B": "Second Base",
    "3B": "Third Base",
    "SS": "Shortstop",
    "LF": "Left Field",
    "CF": "Center Field",
    "RF": "Right Field",
    "DH": "Designated Hitter",
    "OF": "Outfield",
    "IF": "Infield",
    "UT": "Utility",
    "UTIL": "Utility",
}

# The front's line under the name when the stats name no position.
NO_POSITION = "Player"

BATS = {"R": "Bats R", "L": "Bats L", "S": "Switch"}

# The headings over the back's two columns. Every number the card takes is a
# swing metric, and the comps are hitters.
METRICS_HEADING = "Swing metrics"
COMPS_HEADING = "Swings like"

# The steps that run a model on a card. `comps` is a numpy lookup, `compose` is
# Pillow and `render` is Chromium, so none of them has anything to say about
# whether a GPU was involved.
MODEL_STEPS = ("cutout", "art", "text")

MADE_ON_GPU = "Made on Akamai Cloud GPUs. Open weights."
MADE_ON_SOME = "Part GPU, part fallback. See the ran on column."
MADE_ON_CPU = "Open weights on a CPU. No GPU was used."
MADE_ON_MOCK = "Mock run. No GPU was used."

# Anything that is not http(s), ws(s) or ftp is a data: URI we inlined ourselves.
_NETWORK = re.compile(r"^(https?|wss?|ftp)://", re.IGNORECASE)

# Same glyph rasterisation every run, so two renders match byte for byte.
CHROMIUM_ARGS = (
    "--force-color-profile=srgb",
    "--font-render-hinting=none",
    "--disable-font-subpixel-positioning",
    "--disable-lcd-text",
    "--hide-scrollbars",
)

# The pictures on a card's page are hardly compressed. Chromium decodes each
# one once and nobody downloads it, and at Pillow's default PNG level, 6, the
# picture took 1.3 s to encode. A picture with no transparency goes as a BMP,
# 10 ms to write. One with transparency goes as a PNG at zlib level 1, and so
# does a picture inlined as a data: URI. The pixels are the same either way.
PAGE_PNG_LEVEL = 1

# The card's own PNGs come from Chromium's encoder with CDP's optimizeForSpeed
# on: about 13 percent bigger, four times quicker to write, the same pixels.
SCREENSHOT = {"format": "png", "optimizeForSpeed": True}

# A number and the unit after it, in text the model wrote. A line break between
# them leaves "7.1" at the end of one line and "ft" at the start of the next.
_WITH_UNIT = re.compile(r"(-?\d+(?:\.\d+)?) +(mph|ft|ms|deg|degrees?|lb|percent)\b")

# What a file of the card package is inlined as, by its suffix.
_MIME = {".png": "image/png", ".svg": "image/svg+xml"}


@lru_cache(maxsize=1)
def load_styles() -> dict[str, dict]:
    """The styles, in styles.json order. Each one names its colours and the
    prompt its background is painted from."""
    return json.loads(STYLES_PATH.read_text())


def style_name(name: str | None) -> str:
    """The style by that name, or the first one. An unknown name never fails."""
    styles = load_styles()
    return name if name in styles else next(iter(styles))


def get_style(name: str | None) -> dict:
    """A style by name. An unknown name falls back to the first, never fails."""
    return load_styles()[style_name(name)]


@lru_cache(maxsize=1)
def _font_face() -> str:
    """The @font-face block with the font file inlined, so the page needs no network."""
    data = (FONT_DIR / FONT_FILE).read_bytes()
    encoded = base64.b64encode(data).decode("ascii")
    return (
        f"@font-face{{font-family:'{FONT_FAMILY}';font-style:normal;font-weight:100 900;"
        f"font-display:block;src:url(data:font/ttf;base64,{encoded}) format('truetype');}}"
    )


def _css_vars(style: dict) -> str:
    """The style's colours, and the sizes compose.py shares with the page."""
    lines = [f"\t{key}: {value};" for key, value in style["palette"].items()]
    sizes = {
        "--card-w": CARD_W,
        "--card-h": CARD_H,
        "--frame-w": FRAME,
        "--window-w": WINDOW_W,
        "--window-h": WINDOW_H,
        "--picture-h": PICTURE_H,
        "--plate-h": PLATE_H,
    }
    lines += [f"\t{key}: {value}px;" for key, value in sizes.items()]
    return "\n".join(lines)


def data_uri(image: Image.Image) -> str:
    """The picture as a PNG data: URI, for a page that fetches nothing."""
    buffer = io.BytesIO()
    image.save(buffer, "PNG", compress_level=PAGE_PNG_LEVEL)
    return f"data:image/png;base64,{base64.b64encode(buffer.getvalue()).decode('ascii')}"


Picture = Callable[[Image.Image, str], str]
"""Puts a picture where the page can load it, and returns the src for it. The
str it is given names the picture."""


Asset = Callable[[Path], str]
"""Returns the src a page loads a file of the card package from: the logo."""


def inline(image: Image.Image, name: str) -> str:
    """The picture inside the page itself, so the page is one self-contained string."""
    return data_uri(image)


@lru_cache(maxsize=4)
def inline_file(path: Path) -> str:
    """A file of the card package inside the page itself, read once."""
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{_MIME[path.suffix]};base64,{encoded}"


class Folder:
    """A scratch folder a card's pages are written to and opened from.

    Chromium reads a page and its pictures off the disk. Handed to it through
    Playwright's pipe instead, as data: URIs, the front's picture costs a PNG
    encode and about 40 ms a megabyte to move. With the picture at Pillow's
    default PNG level, building and loading the front took 1.9 s that way. From
    a folder, with the picture as a BMP, it takes about 0.1 s.
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    def picture(self, image: Image.Image, name: str) -> str:
        """Write the picture beside the pages and return its file name."""
        if image.mode == "RGB":
            path = self.root / f"{name}.bmp"
            image.save(path, "BMP")
        else:
            path = self.root / f"{name}.png"
            image.save(path, "PNG", compress_level=PAGE_PNG_LEVEL)
        return path.name

    def page(self, side: str, html: str) -> str:
        """Write one side's page here and return the file: URL to open it at."""
        path = self.root / f"{side}.html"
        path.write_text(html, encoding="utf-8")
        return path.as_uri()

    @staticmethod
    def file(path: Path) -> str:
        """Where a page in the folder loads a file of the card package from:
        where it is. A file: page may show a file: picture."""
        return path.as_uri()


def metric_rows(metrics: dict[str, float]) -> list[dict[str, str]]:
    """One row per metric that was supplied, in the order it came in."""
    rows = []
    for key, value in metrics.items():
        metric = METRICS[key]
        rows.append(
            {"label": metric.label, "value": f"{value:.{metric.decimals}f}", "unit": metric.unit}
        )
    return rows


def keep_units(text: str) -> Markup:
    """The text, escaped, with each number and its unit held together on one line."""
    return Markup(_WITH_UNIT.sub(r'<span class="nb">\1 \2</span>', str(escape(text))))


def made_line(steps: Sequence[Step]) -> str:
    """The one claim the card makes about where it was made, checked against the ledger.

    It is whatever the steps that ran a model reported, and there are four
    honest answers:

      every model step on a GPU        the Akamai line
      some of them                     say so, and point at the column. An art
                                       step that fell back to a background
                                       painted on the CPU reports akamai-cpu,
                                       and a card that cost a GPU hour should
                                       not read "mock run"
      none on a GPU, some real         a real backend run on a CPU. No GPU
                                       touched it and it is not a mock either
      nothing but mocks                mock run. The laptop stack labels its
                                       mocks by the GPU each one stands in for,
                                       mock-gpu0, and a mock on a pretend GPU is
                                       still a mock

    A card with no finished model step at all claims nothing.
    """
    ran = [step for step in steps if step.step in MODEL_STEPS and step.status == "done"]
    on_gpu = [(step.ran_on or "").startswith("akamai-gpu") for step in ran]

    if on_gpu and all(on_gpu):
        return MADE_ON_GPU
    if any(on_gpu):
        return MADE_ON_SOME
    if any(not (step.ran_on or "mock").startswith("mock") for step in ran):
        return MADE_ON_CPU
    return MADE_ON_MOCK


def _height(inches: int | None) -> str | None:
    if not inches:
        return None
    # Real prime characters. The template escapes its inputs, so entities would
    # show up on the card as literal text.
    return f"{inches // 12}′ {inches % 12}″"


def bio_items(stats: Stats) -> list[str]:
    """The bio row on the back. Anything the stats left out is left out here."""
    items = [
        BATS.get(stats.bats or ""),
        f"Throws {stats.throws}" if stats.throws else None,
        _height(stats.height_in),
        f"{stats.weight_lb} lb" if stats.weight_lb else None,
        stats.hometown,
    ]
    return [item for item in items if item]


def position_line(stats: Stats) -> str:
    """The line under the name on the front: the position spelled out, or Player."""
    return POSITIONS.get(stats.position or "", stats.position or "") or NO_POSITION


@lru_cache(maxsize=1)
def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _page(style: dict, side: str, name: str) -> dict:
    return {
        "side": side,
        "name": name,
        "font_face": _font_face(),
        "css_vars": _css_vars(style),
        "base_css": CSS_PATH.read_text(),
    }


def front_html(
    stats: Stats,
    style: str | None,
    hero: Image.Image,
    picture: Picture = inline,
    asset: Asset = inline_file,
) -> str:
    """The front: the picture, and the name plate under it.

    `picture` puts the picture where the page loads it from, and `asset` a file
    of the card package, the logo: both inside the page by default, beside it
    or where it is from a Folder. A template asks for a brand file by name,
    `brand("akamai-logo-white.svg")`, and only the template that shows it asks.
    The name is as the stats give it, and data-fit shrinks one too wide for
    the plate. The position is spelled out, and is "Player" when the stats have
    none.
    """
    name = style_name(style)
    context = _page(get_style(name), "side-front", stats.name)
    context.update(
        hero=picture(hero, "hero"),
        brand=lambda file: asset(BRAND_DIR / file),
        number=stats.number,
        role=position_line(stats),
    )
    return _env().get_template(f"front-{name}.html").render(**context)


def back_html(
    stats: Stats,
    style: str | None,
    text: CardText,
    comps: Sequence[Comp],
    steps: Sequence[Step],
) -> str:
    """The back: the bio, what the model wrote, the numbers, the comps and the ledger."""
    context = _page(get_style(style), "side-back", stats.name)
    context.update(
        number=stats.number,
        bio=bio_items(stats),
        text=text,
        tagline=keep_units(text.tagline),
        blurb=keep_units(text.blurb),
        metrics=metric_rows(stats.metrics),
        metrics_heading=METRICS_HEADING,
        comps_heading=COMPS_HEADING,
        comps=[
            {
                "player": comp.player,
                "season": comp.season,
                "percent": round(comp.similarity * 100),
            }
            for comp in comps
        ],
        # Only steps that finished and named a model. A compose or render step
        # has no model, and a failed step has no honest line to show.
        steps=[step for step in steps if step.model and step.status == "done"],
        made=made_line(steps),
    )
    return _env().get_template("back.html").render(**context)


class Chromium:
    """One headless Chromium, started for the first card and kept for the next.

    Starting Playwright and Chromium takes about 0.3 s, most of a render once
    the rest is quick, so the browser outlives the card. A card gets a
    browser context of its own, a page for each side, and all of it is closed
    when the card is done. A browser that has died is started again for the
    next card.

    Playwright's objects belong to the event loop that made them. The browser
    lives on a thread of its own with its own loop, and run() hands it work
    from any other thread and waits for the answer.
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._launch: asyncio.Lock | None = None
        self._busy: set[asyncio.Task] = set()
        self._playwright: Any = None
        self._browser: Any = None

    def run(self, work: Callable[[Any], Awaitable[Any]]) -> Any:
        """`await work(browser)` on the browser's thread. Blocks until it is done."""
        loop = self._started()
        if threading.current_thread() is self._thread:
            raise RuntimeError("run() waits on the browser's thread, so it cannot run on it")
        return asyncio.run_coroutine_threadsafe(self._with_browser(work), loop).result()

    def _started(self) -> asyncio.AbstractEventLoop:
        with self._guard:
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                self._launch = asyncio.Lock()
                self._thread = threading.Thread(
                    target=self._loop.run_forever, name="chromium", daemon=True
                )
                self._thread.start()
            return self._loop

    async def _with_browser(self, work: Callable[[Any], Awaitable[Any]]) -> Any:
        task = asyncio.current_task()
        self._busy.add(task)
        try:
            async with self._launch:
                if self._browser is None or not self._browser.is_connected():
                    if self._playwright is None:
                        from playwright.async_api import async_playwright

                        self._playwright = await async_playwright().start()
                    self._browser = await self._playwright.chromium.launch(args=list(CHROMIUM_ARGS))
            return await work(self._browser)
        finally:
            self._busy.discard(task)

    def close(self) -> None:
        """Close the browser and end its thread. The next card starts both again.

        A card still being drawn is cancelled first, so nobody waits for ever.
        """
        with self._guard:
            loop, thread = self._loop, self._thread
            self._loop = self._thread = self._launch = None
        if loop is None or thread is None:
            return

        async def stop() -> None:
            busy = list(self._busy)
            for task in busy:
                task.cancel()
            await asyncio.gather(*busy, return_exceptions=True)
            with contextlib.suppress(Exception):
                if self._browser is not None:
                    await self._browser.close()
            with contextlib.suppress(Exception):
                if self._playwright is not None:
                    await self._playwright.stop()
            self._browser = self._playwright = None

        try:
            asyncio.run_coroutine_threadsafe(stop(), loop).result(timeout=20)
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5)
            if not thread.is_alive():
                loop.close()


# The browser render_card() draws in unless it is handed another. It starts
# with the first card. The api closes it when it shuts down, and anything else
# when its process exits.
CHROMIUM = Chromium()
atexit.register(CHROMIUM.close)


def write_file(path: Path, data: bytes) -> Path:
    """Write beside `path`, then rename, so no reader ever sees half a file."""
    part = path.with_name(f".{path.name}.part")
    part.write_bytes(data)
    part.replace(path)
    return path


def preview_jpeg(front: bytes) -> bytes:
    """The front's PNG as a 500 x 700 JPEG."""
    with Image.open(io.BytesIO(front)) as opened:
        preview = opened.convert("RGB")
    preview.thumbnail((PREVIEW_W, PREVIEW_W * CARD_H // CARD_W), Image.LANCZOS)
    buffer = io.BytesIO()
    preview.save(buffer, "JPEG", quality=86, optimize=True)
    return buffer.getvalue()


async def screenshot(page) -> bytes:
    """The page as a PNG, from Chromium's quick encoder."""
    cdp = await page.context.new_cdp_session(page)
    try:
        shot = await cdp.send("Page.captureScreenshot", SCREENSHOT)
    finally:
        await cdp.detach()
    return base64.b64decode(shot["data"])


async def draw_card(browser, sides: dict[str, Callable[[], str]], paths: dict[str, Path]) -> None:
    """Every side at once, each in a page of its own, each written when it is done.

    `sides` writes each side's page, on a worker thread, and returns the URL to
    open it at. `paths` says where each file goes. The preview is written
    straight after the front, from the same bytes, while the back may still be
    drawing. If one side fails, the other is stopped, and the first failure is
    what the caller sees.

    The pages share one browser context, closed when the card is done however
    it ended, so a card leaves nothing open in the browser that outlives it.
    """
    blocked: list[str] = []

    async def refuse(route) -> None:
        blocked.append(route.request.url)
        await route.abort()

    context = await browser.new_context(
        viewport={"width": CARD_W, "height": CARD_H}, device_scale_factor=1
    )

    async def side(name: str, build: Callable[[], str]) -> None:
        page = await context.new_page()
        url = await asyncio.to_thread(build)
        await page.goto(url, wait_until="load")
        await page.wait_for_function("window.__cardReady === 1", timeout=30_000)
        png = await screenshot(page)
        if blocked:
            raise RuntimeError(f"the card template tried to reach the network: {blocked[0]}")
        await asyncio.to_thread(write_file, paths[name], png)
        if name == "front" and "preview" in paths:
            preview = await asyncio.to_thread(preview_jpeg, png)
            await asyncio.to_thread(write_file, paths["preview"], preview)

    try:
        await context.route(_NETWORK, refuse)
        tasks = [asyncio.ensure_future(side(name, build)) for name, build in sides.items()]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
    finally:
        await context.close()


def file_names(basename: str) -> dict[str, str]:
    """What render_card() calls its files unless it is told otherwise."""
    return {
        "front": f"{basename}-front.png",
        "back": f"{basename}-back.png",
        "preview": f"{basename}-preview.jpg",
    }


def render_card(
    stats: Stats,
    *,
    style: str | None,
    art: Image.Image,
    cutout: Image.Image | None = None,
    out_dir: Path,
    text: CardText | None = None,
    comps: Sequence[Comp] = (),
    steps: Sequence[Step] = (),
    basename: str = "card",
    face: Box | None = None,
    names: dict[str, str] | None = None,
    chromium: Chromium | None = None,
) -> dict[str, Path]:
    """Compose the front picture, render both sides, and write a preview.

    `face` is the face box in the photo's pixels, which the cutout shares. It
    places the person. Without a cutout, `art` is the picture as it is: the
    pipeline hands in one it composed.

    `names` gives the file name for the front, the back and the preview in
    `out_dir`, and defaults to <basename>-front.png and so on. Each file appears
    under its name whole, the moment it is done: the front and its preview
    first, while the back is still being drawn. `chromium` is the browser to
    draw in, CHROMIUM unless another is given.

    Returns {"front": ..., "back": ..., "preview": ...}.
    """
    hero = compose_hero(art, cutout, face=face)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {side: out_dir / name for side, name in (names or file_names(basename)).items()}
    with tempfile.TemporaryDirectory(prefix="card-") as scratch:
        folder = Folder(Path(scratch))
        sides = {
            "front": lambda: folder.page(
                "front", front_html(stats, style, hero, folder.picture, folder.file)
            ),
            "back": lambda: folder.page(
                "back", back_html(stats, style, text or CardText(), comps, steps)
            ),
        }
        (chromium or CHROMIUM).run(lambda browser: draw_card(browser, sides, paths))
    return paths


# ---------------------------------------------------------------------------
# --fixture: render from the files in the repo, with no api and no models.
# ---------------------------------------------------------------------------

# packages/card/src/card/render.py -> the repo root. Only the fixture render
# reads it, for the fixture cutout and examples/stats.json. Not used in an image.
REPO_ROOT = Path(__file__).resolve().parents[4]
FIXTURE_CUTOUT = REPO_ROOT / "tests" / "fixtures" / "cutout.png"
FIXTURE_STATS = REPO_ROOT / "examples" / "stats.json"

# The browser face detector's box on the fixture portrait, in its 1024 x 683
# pixels, which the fixture cutout shares.
FIXTURE_FACE = (458, 75, 123, 123)

# The first example back in the text prompt, word for word. The fixture player
# has the same numbers, so the back reads the way the prompt asks the model to
# write.
FIXTURE_TEXT = CardText(
    nickname="Short Fuse",
    tagline="Quick to the ball, quicker to first",
    blurb=(
        "Sam gets the bat to the ball in 155 ms, a short swing at 7.1 ft. "
        "The 88.5 mph exit velocity will not clear many fences. "
        "The 14 degree launch angle sends it over the infield. Doubles, not homers."
    ),
)

# Three made-up hitters. Comps from the Statcast table name real ones.
FIXTURE_COMPS = [
    Comp(player="A. Vargas", season=2025, similarity=0.94),
    Comp(player="M. Okonkwo", season=2025, similarity=0.88),
    Comp(player="T. Brennan", season=2025, similarity=0.81),
]

# The ledger of the stand-ins that made the fixture's pictures: the plain-wall
# matte cut tests/fixtures/cutout.png, and the art is the drawn stadium. So the
# card says it is a mock run, which is true. The cutout and art times are what
# the two stand-ins took on a laptop CPU. The comps and text rows are a sample,
# like the comps and the text above.
FIXTURE_STEPS = [
    Step(
        step="cutout",
        status="done",
        model="mock/plain-wall-matte",
        license="n/a",
        ran_on="mock-gpu0",
        ms=272,
    ),
    Step(
        step="art",
        status="done",
        model="mock/procedural-stadium",
        license="n/a",
        ran_on="mock-gpu0",
        ms=204,
        source="pool",
    ),
    Step(
        step="comps", status="done", model="nearest neighbour", license="n/a", ran_on="mock", ms=6
    ),
    Step(
        step="text",
        status="done",
        model="mock/wordlist",
        license="n/a",
        ran_on="mock-gpu0",
        ms=10,
    ),
]


def fixture_stats() -> Stats:
    return Stats.model_validate_json(FIXTURE_STATS.read_text())


def render_fixture(out_dir: Path, style: str | None = None, basename: str = "card") -> dict:
    """Render one card from the repo fixtures. No api, no models, no network."""
    from card.procedural import stadium

    with Image.open(FIXTURE_CUTOUT) as opened:
        cutout = opened.convert("RGBA")
    return render_card(
        fixture_stats(),
        style=style,
        art=stadium(style_name(style), 1024, 1408, seed=11),
        cutout=cutout,
        out_dir=out_dir,
        text=FIXTURE_TEXT,
        comps=FIXTURE_COMPS,
        steps=FIXTURE_STEPS,
        basename=basename,
        face=FIXTURE_FACE,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render a baseball card to PNG.")
    parser.add_argument("--fixture", action="store_true", help="render the repo fixture card")
    parser.add_argument("--style", default=None, help=f"one of: {', '.join(load_styles())}")
    parser.add_argument("--all-styles", action="store_true", help="one card per style")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "out", help="output directory")
    args = parser.parse_args(argv)

    if not args.fixture:
        parser.error("nothing to render. Pass --fixture.")
    if args.style is not None and args.style not in load_styles():
        parser.error(f"no style {args.style!r}. Use one of: {', '.join(load_styles())}")

    if args.all_styles:
        for name in load_styles():
            written = render_fixture(args.out, name, basename=name)
            print(f"{name}: {written['front'].name}, {written['back'].name}")
    else:
        written = render_fixture(args.out, args.style)
        for side in ("front", "back", "preview"):
            print(written[side])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
