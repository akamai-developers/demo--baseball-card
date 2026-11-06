"""The browser app, run in a browser, against the mock stack through Caddy.

Sign in, the test photo and the sample player, Generate, the card's front
before its back, flip, both sides saved at print size, Make another, the node
panel and How it works. It runs the real api, the real pipeline and the real
renderer, so what the page shows is what the stack did.

It also holds what docs/SECURITY.md says about the page: the key is never in
a URL, every request goes to the page's own origin, and a key the node stops
taking signs the tab out.

The screenshots land in out/ui/ at 1920 x 1080 and at a phone's 390 x 844.
out/ is not committed, so look at them after a change to the layout.

Skips itself when the stack is not up. Start it with: make up-mock
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from email.parser import BytesParser
from email.policy import HTTP
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from PIL import Image

from tests.conftest import REPO_ROOT, confirm_key, refute_key

pytestmark = pytest.mark.integration

PORTRAIT = REPO_ROOT / "tests" / "fixtures" / "portrait.jpg"
SAMPLE = json.loads((REPO_ROOT / "examples" / "stats.json").read_text())
SHOTS = REPO_ROOT / "out" / "ui"

DESKTOP = {"width": 1920, "height": 1080}
PHONE = {"width": 390, "height": 844}

# Six steps and a Chromium render on the mock stack. A few seconds, with room.
CARD_MS = 60_000

# GET /cards/{id}, the job. Not its pictures.
JOB = re.compile(r"/cards/[A-Za-z0-9_-]{22}$")

# MediaPipe's wasm build writes its log lines to stderr, which Emscripten hands
# to console.error. This one says the face detector chose its CPU path.
NOT_ERRORS = ("INFO: Created TensorFlow Lite XNNPACK delegate for CPU.",)

# Every animation and transition has ended, but for the running dot's
# heartbeat, which never does.
SETTLED = """() => document.getAnimations().every(
  (a) => a.playState !== "running" || a.effect.getComputedTiming().iterations === Infinity)"""


@pytest.fixture(scope="module")
def browser() -> Iterator[object]:
    playwright_api = pytest.importorskip("playwright.sync_api")
    with playwright_api.sync_playwright() as playwright:
        instance = playwright.chromium.launch()
        yield instance
        instance.close()


class Watched:
    """A page, with every console error, page error and request URL it made."""

    def __init__(self, page) -> None:
        self.page = page
        self.errors: list[str] = []
        self.urls: list[str] = []
        page.on("console", self.console)
        page.on("pageerror", lambda error: self.errors.append(str(error)))
        page.on("request", lambda request: self.urls.append(request.url))

    def console(self, message) -> None:
        if message.type == "error" and message.text not in NOT_ERRORS:
            self.errors.append(message.text)


@pytest.fixture
def watched(browser) -> Iterator[Watched]:
    context = browser.new_context(viewport=DESKTOP, accept_downloads=True)
    page = context.new_page()
    yield Watched(page)
    context.close()


def sign_in(page, gateway: str, key: str) -> None:
    page.goto(f"{gateway}/", wait_until="domcontentloaded")
    page.wait_for_selector("#signin:not([hidden])")
    page.fill("#key-input", key)
    page.click("#signin-btn")
    page.wait_for_selector("#app:not([hidden])", timeout=20_000)


def choose_photo(page, path: Path = PORTRAIT) -> None:
    """Upload a photo and wait while the browser shrinks it and finds the face."""
    page.set_input_files("#file-input", str(path))
    page.wait_for_selector("#thumb-row:not([hidden])", timeout=30_000)


def generate(page) -> None:
    page.click("#generate-btn")
    page.wait_for_selector("#result:not([hidden])", timeout=20_000)


def wait_for_card(page) -> None:
    page.wait_for_function(
        "document.getElementById('result-status').textContent.startsWith('Made in')",
        timeout=CARD_MS,
    )
    page.wait_for_function("!document.getElementById('save-back').disabled", timeout=CARD_MS)


def shot(page, name: str, **options) -> None:
    """A screenshot in out/ui/, once nothing is moving."""
    page.wait_for_function(SETTLED)
    page.screenshot(path=str(SHOTS / name), **options)


def form_fields(request) -> dict[str, bytes]:
    """A multipart/form-data body, field by field."""
    head = f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode()
    message = BytesParser(policy=HTTP).parsebytes(head + request.post_data_buffer)
    return {
        part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
        for part in message.iter_parts()
    }


def png_size(path: Path) -> tuple[int, int]:
    with Image.open(path) as image:
        assert image.format == "PNG"
        return image.size


# -------------------------------------------------------------- sign in --


def test_the_sign_in_screen_refuses_a_wrong_key_and_takes_the_right_one(
    watched: Watched, gateway: str, live_key: str
) -> None:
    page = watched.page
    page.goto(f"{gateway}/", wait_until="domcontentloaded")
    page.wait_for_selector("#signin:not([hidden])")
    assert page.locator("#app").is_hidden()
    shot(page, "signin-1920x1080.png")

    page.fill("#key-input", "w" * 64)
    page.click("#signin-btn")
    page.wait_for_selector("#signin-error:not([hidden])")
    assert "wrong" in page.inner_text("#signin-error")
    assert "CARD_API_KEY" in page.inner_text("#signin-error")
    assert page.locator("#app").is_hidden()

    page.fill("#key-input", live_key)
    page.click("#signin-btn")
    page.wait_for_selector("#app:not([hidden])", timeout=20_000)
    confirm_key(page.evaluate("sessionStorage.getItem('card-api-key')"), live_key, "sessionStorage")
    # Nothing else of the key is kept: no cookie, no localStorage, and the
    # field it was typed into is empty.
    assert page.evaluate("document.cookie") == ""
    assert page.evaluate("localStorage.length") == 0
    assert page.input_value("#key-input") == ""
    # The header says what the node runs, from /healthz.
    page.wait_for_function(
        "document.getElementById('node-pill-text').textContent.includes('loaded')"
    )
    assert page.inner_text("#node-pill-text") == "3 of 3 loaded, mock"


def test_a_reload_keeps_the_tab_signed_in_and_sign_out_forgets_the_key(
    watched: Watched, gateway: str, live_key: str
) -> None:
    page = watched.page
    sign_in(page, gateway, live_key)
    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector("#app:not([hidden])", timeout=20_000)
    page.click("#signout-btn")
    page.wait_for_selector("#signin:not([hidden])")
    assert page.evaluate("sessionStorage.getItem('card-api-key')") is None


def test_a_key_the_node_stops_taking_signs_the_tab_out(
    watched: Watched, gateway: str, live_key: str
) -> None:
    """As after make rotate-key: the next call is a 401, and the tab says why."""
    page = watched.page
    sign_in(page, gateway, live_key)
    page.route(
        "**/gpu",
        lambda route: route.fulfill(status=401, json={"detail": "missing or invalid API key"}),
    )
    page.click('#nav [data-view="node"]')
    page.wait_for_selector("#signin:not([hidden])", timeout=20_000)
    assert "rotated" in page.inner_text("#signin-error")
    assert page.evaluate("sessionStorage.getItem('card-api-key')") is None


# ------------------------------------------------------------- the card --


def test_a_card_from_the_test_photo(
    watched: Watched, gateway: str, live_key: str, mock_backends
) -> None:
    """The whole path at 1920 x 1080, with a screenshot of each view."""
    page = watched.page
    sign_in(page, gateway, live_key)

    # Generate waits for a photo and a name, and says so.
    assert page.is_disabled("#generate-btn")
    assert page.inner_text("#generate-note") == "Add a photo and a name to start."
    choose_photo(page)
    assert page.inner_text("#thumb-size").startswith("1024 x 683")
    assert page.inner_text("#generate-note") == "Type a name. It goes on the front."
    page.click("#sample-btn")
    assert page.input_value("#f-name") == "Sam Rivera"
    assert page.inner_text("#metrics-note") == "6 of 6 on"
    assert page.is_enabled("#generate-btn")
    assert page.is_checked('#styles input[value="akamai"]')
    shot(page, "make-1920x1080.png")

    generate(page)
    assert page.locator("#make-form").is_hidden()
    assert page.inner_text("#result-title") == "Sam Rivera, Akamai"
    assert page.evaluate("document.activeElement.id") == "result-title"
    wait_for_card(page)

    # The ledger: six steps, each with what its service said about itself.
    rows = page.locator("#result-ledger tr")
    assert rows.count() == 6
    steps = [row.get_attribute("data-step") for row in rows.all()]
    assert steps == ["cutout", "art", "comps", "text", "compose", "render"]
    assert all(row.get_attribute("data-status") == "done" for row in rows.all())
    assert page.inner_text('#result-ledger tr[data-step="cutout"] .lg-model') == "plain-wall-matte"
    # From the art service's pool, or painted for this card when none was ready.
    assert re.fullmatch(
        r"procedural-stadium · (pool|fresh)",
        page.inner_text('#result-ledger tr[data-step="art"] .lg-model'),
    )
    assert page.inner_text('#result-ledger tr[data-step="text"] .lg-where') == "mock-gpu0"
    assert re.fullmatch(r"\d+ ms", page.inner_text('#result-ledger tr[data-step="render"] .lg-ms'))
    # The test photo is placed by its face, with nothing to warn about.
    assert page.locator("#result-warnings").is_hidden()

    # The front is up, at print size.
    page.wait_for_function("document.getElementById('card-front').naturalWidth === 1500")
    shot(page, "result-front-1920x1080.png")

    # A click turns it over to the back, and the back is drawn.
    page.click("#card-flip")
    page.wait_for_selector("#card-flip.is-turned")
    assert page.get_attribute("#result-flip", "aria-pressed") == "true"
    page.wait_for_function(
        "(() => { const img = document.getElementById('card-back');"
        " return !img.hidden && img.naturalWidth === 1500 && img.naturalHeight === 2100; })()"
    )
    shot(page, "result-back-1920x1080.png")
    page.click("#result-flip")
    page.wait_for_selector("#card-flip:not(.is-turned)")

    # The node panel has the card's ledger, and How it works the models.
    page.click('#nav [data-view="node"]')
    page.wait_for_selector("#node-gpus .gpu")
    page.wait_for_selector("#node-last:not([hidden])")
    shot(page, "node-1920x1080.png")
    page.click('#nav [data-view="how"]')
    page.wait_for_selector("#model-table tr")
    shot(page, "how-1920x1080.png")

    assert watched.errors == []


def test_the_front_shows_while_the_back_is_written(
    watched: Watched, gateway: str, live_key: str, mock_backends
) -> None:
    """The job lists front.png and preview.jpg before back.png. Held there, the
    front is up and the back says it is being written. Let go, the back comes."""
    page = watched.page
    hold = {"on": True}

    def job(route) -> None:
        response = route.fetch()
        body = response.json()
        # The back can be listed a moment before the job says done, so every
        # answer loses it while the hold is on.
        if hold["on"]:
            body["assets"] = [name for name in body["assets"] if name != "back.png"]
            if body.get("status") == "done":
                body["status"] = "running"
                body["steps"][-1].update(status="running", ms=None)
        route.fulfill(response=response, json=body)

    page.route(JOB, job)
    sign_in(page, gateway, live_key)
    choose_photo(page)
    page.click("#sample-btn")
    generate(page)

    page.wait_for_selector("#card-flip:not([hidden])", timeout=CARD_MS)
    page.wait_for_function("document.getElementById('card-front').naturalWidth > 0")
    assert page.inner_text("#result-status").startswith("Writing the back")
    assert page.locator("#card-back").is_hidden()
    assert page.is_disabled("#save-back")
    page.click("#card-flip")
    page.wait_for_selector("#card-flip.is-turned")
    assert page.inner_text("#card-back-wait") == "Writing the back"

    hold["on"] = False
    wait_for_card(page)
    page.wait_for_function("document.getElementById('card-back').naturalWidth === 1500")
    assert page.locator("#card-back-wait").is_hidden()


def test_a_picture_whose_fetch_fails_is_asked_for_again(
    watched: Watched, gateway: str, live_key: str, mock_backends
) -> None:
    """The back's first fetch dies on the wire after the job has ended. The
    page asks again, and Save back works."""
    page = watched.page
    dropped: list[str] = []

    def back(route) -> None:
        if not dropped:
            dropped.append(route.request.url)
            route.abort()
        else:
            route.continue_()

    page.route("**/back.png", back)
    sign_in(page, gateway, live_key)
    choose_photo(page)
    page.fill("#f-name", "Sam Rivera")
    generate(page)
    wait_for_card(page)
    assert len(dropped) == 1
    page.wait_for_function("document.getElementById('card-back').naturalWidth === 1500")


def test_save_front_and_save_back_write_the_pngs_at_print_size(
    watched: Watched, gateway: str, live_key: str, tmp_path: Path, mock_backends
) -> None:
    page = watched.page
    sign_in(page, gateway, live_key)
    choose_photo(page)
    page.click("#sample-btn")
    page.click('#styles .style:has(input[value="classic"])')
    generate(page)
    wait_for_card(page)
    assert page.inner_text("#result-title") == "Sam Rivera, Classic"

    for button, name in (
        ("#save-front", "sam-rivera-classic-front.png"),
        ("#save-back", "sam-rivera-classic-back.png"),
    ):
        with page.expect_download() as info:
            page.click(button)
        download = info.value
        assert download.suggested_filename == name
        target = tmp_path / name
        download.save_as(target)
        assert png_size(target) == (1500, 2100)


def test_make_another_brings_the_form_back_as_it_was(
    watched: Watched, gateway: str, live_key: str, mock_backends
) -> None:
    page = watched.page
    sign_in(page, gateway, live_key)
    choose_photo(page)
    page.fill("#f-name", "Ka'Ni Quillfeather")
    generate(page)
    wait_for_card(page)

    page.click("#result-again")
    page.wait_for_selector("#make-form:not([hidden])")
    assert page.locator("#result").is_hidden()
    assert page.input_value("#f-name") == "Ka'Ni Quillfeather"
    assert page.is_visible("#thumb-row")
    assert page.is_enabled("#generate-btn")
    # The focus is back on step 1, where the photo can be changed.
    assert page.evaluate("document.activeElement.id") == "retake-btn"

    # The node panel shows the card again from its ledger.
    page.click('#nav [data-view="node"]')
    page.wait_for_selector("#node-last:not([hidden])")
    assert page.inner_text("#node-last-title") == "Ka'Ni Quillfeather, Akamai, made"
    page.click("#node-last-show")
    page.wait_for_selector("#result:not([hidden])")
    assert page.evaluate("location.hash") == "#make"


def test_the_post_carries_what_was_typed_and_nothing_else(
    watched: Watched, gateway: str, live_key: str, mock_backends
) -> None:
    """stats is the fields filled in and the numbers ticked. A card with no
    numbers ticked sends no metrics, so the back has no table and no comps."""
    page = watched.page
    posted: list[dict[str, bytes]] = []

    def capture(route) -> None:
        if route.request.method == "POST":
            posted.append(form_fields(route.request))
        route.continue_()

    page.route("**/cards", capture)
    sign_in(page, gateway, live_key)
    choose_photo(page)
    page.fill("#f-name", "Sam Rivera")
    page.fill("#f-number", "7")
    page.select_option("#f-position", "CF")
    generate(page)
    wait_for_card(page)

    page.click("#result-again")
    page.wait_for_selector("#make-form:not([hidden])")
    page.click("#sample-btn")
    generate(page)
    wait_for_card(page)

    plain, full = posted
    assert json.loads(plain["stats"]) == {"name": "Sam Rivera", "number": 7, "position": "CF"}
    assert plain["style"] == b"akamai"
    # The sample is examples/stats.json, field for field.
    assert json.loads(full["stats"]) == SAMPLE
    # The photo went as the JPEG the browser made, 1024 px wide like the test
    # photo, and the face as whole pixels inside it.
    with Image.open(BytesIO(full["photo"])) as photo:
        assert photo.format == "JPEG"
        assert photo.size == (1024, 683)
    face = json.loads(full["face"])
    assert set(face) == {"x", "y", "width", "height"}
    assert all(isinstance(value, int) for value in face.values())
    assert face["x"] + face["width"] <= 1024 and face["y"] + face["height"] <= 683


def test_a_number_the_api_refuses_comes_back_as_its_sentence(
    watched: Watched, gateway: str, live_key: str
) -> None:
    page = watched.page
    sign_in(page, gateway, live_key)
    choose_photo(page)
    page.fill("#f-name", "Sam Rivera")
    page.fill("#f-number", "150")
    page.click("#generate-btn")
    page.wait_for_function(
        "document.getElementById('generate-note').classList.contains('is-error')"
    )
    note = page.inner_text("#generate-note")
    assert note.startswith("Stats is not usable")
    assert "99" in note
    assert page.is_visible("#make-form")


def test_a_file_that_is_not_an_image_says_so(
    watched: Watched, gateway: str, live_key: str, tmp_path: Path
) -> None:
    page = watched.page
    sign_in(page, gateway, live_key)
    notes = tmp_path / "notes.txt"
    notes.write_text("not a photo")
    page.set_input_files("#file-input", str(notes))
    page.wait_for_selector("#photo-error:not([hidden])")
    assert (
        page.inner_text("#photo-error") == "notes.txt is not an image. Choose a JPEG, PNG or WebP."
    )
    assert page.locator("#thumb-row").is_hidden()


# --------------------------------------------------------- node and how --


def test_the_node_panel_shows_the_models_and_the_gpus(
    watched: Watched, gateway: str, live_key: str, mock_backends
) -> None:
    page = watched.page
    sign_in(page, gateway, live_key)
    page.click('#nav [data-view="node"]')
    page.wait_for_selector("#node-gpus .gpu")
    # The names come from GET /models, which can land after the first paint.
    cutout = "document.querySelector('#node-services [data-row=cutout]')"
    page.wait_for_function(f"{cutout}.textContent.includes('BiRefNet')")

    rows = {row.get_attribute("data-row"): row for row in page.locator("#node-services tr").all()}
    assert list(rows) == ["api", "cutout", "art", "llm", "gpu-stats"]
    assert rows["cutout"].inner_text().startswith("cutout, BiRefNet")
    assert "mock/procedural-stadium" in rows["art"].inner_text()
    assert "GPU 0, mock" in rows["llm"].inner_text()
    # gpu-stats in mock mode invents one card, and says it is a mock.
    assert page.inner_text("#node-gpus .gpu-name") == "GPU 0, Mock RTX 4000 Ada"
    assert re.fullmatch(
        r"\d+\.\d of 20\.0 GB", page.locator("#node-gpus .gpu-value").nth(1).inner_text()
    )
    assert page.inner_text("#node-last-none") == "No card made in this tab yet."
    assert watched.errors == []


def test_how_it_works_reads_the_models_from_the_api(
    watched: Watched, gateway: str, live_key: str, mock_backends
) -> None:
    page = watched.page
    sign_in(page, gateway, live_key)
    page.click('#nav [data-view="how"]')
    page.wait_for_selector("#model-table tr")

    lines = page.locator("#how-steps li").all_inner_texts()
    assert len(lines) == 5
    assert lines[1] == (
        "BiRefNet cuts you out of the photo while FLUX.2 [klein] 4B paints the stadium behind you."
    )
    assert "Gemma 4 12B IT" in lines[2]

    models = httpx.get(
        f"{gateway}/models", headers={"Authorization": f"Bearer {live_key}"}, timeout=10
    ).json()
    rows = page.locator("#model-table tr").all()
    assert len(rows) == len(models["models"]) == 3
    for row, model in zip(rows, models["models"], strict=True):
        assert row.locator(".mt-name").inner_text() == model["name"]
        assert row.locator(".mt-licence").inner_text() == model["license"]
        assert row.locator(".mt-pin").inner_text() == model["revision"][:12]
        repo = row.locator(".mt-repo")
        assert repo.inner_text() == model["repo"]
        assert repo.get_attribute("href") == model["url"]
        assert repo.get_attribute("rel") == "noopener noreferrer"
    assert page.is_visible("#how-mock")


# ----------------------------------------------------------- the promise --


def test_the_key_is_never_in_a_url_and_nothing_leaves_the_origin(
    watched: Watched, gateway: str, live_key: str, mock_backends
) -> None:
    """Every request of a whole visit: sign in, a card, every view."""
    page = watched.page
    sign_in(page, gateway, live_key)
    choose_photo(page)
    page.click("#sample-btn")
    generate(page)
    wait_for_card(page)
    page.click("#card-flip")
    page.click('#nav [data-view="node"]')
    page.wait_for_selector("#node-gpus .gpu")
    page.click('#nav [data-view="how"]')
    page.wait_for_selector("#model-table tr")

    assert len(watched.urls) > 20
    for url in watched.urls:
        refute_key(url, live_key, "a request URL")
        assert url.startswith((f"{gateway}/", "blob:", "data:")), url
    refute_key(page.url, live_key, "the address bar")
    assert watched.errors == []


# ---------------------------------------------------------------- phone --


def test_the_phone_layout_fits_the_screen(
    browser, gateway: str, live_key: str, mock_backends
) -> None:
    """No view is wider than a 390 px phone, and the screenshots say the rest."""
    context = browser.new_context(
        viewport=PHONE, device_scale_factor=2, is_mobile=True, has_touch=True
    )
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    fits = "document.documentElement.scrollWidth <= window.innerWidth"
    try:
        page.goto(f"{gateway}/", wait_until="domcontentloaded")
        page.wait_for_selector("#signin:not([hidden])")
        shot(page, "signin-390x844.png")
        sign_in(page, gateway, live_key)
        choose_photo(page)
        page.click("#sample-btn")
        page.locator(".more summary").nth(1).click()
        assert page.evaluate(fits)
        shot(page, "make-390x844.png", full_page=True)

        page.click("#generate-btn")
        wait_for_card(page)
        page.wait_for_function("document.getElementById('card-front').naturalWidth === 1500")
        assert page.evaluate(fits)
        # Generate was at the foot of a long form. The card opens at its top.
        assert page.evaluate("window.scrollY") == 0
        shot(page, "result-390x844.png", full_page=True)

        for view in ("node", "how"):
            page.click(f'#nav [data-view="{view}"]')
            page.wait_for_selector(f"#view-{view}:not([hidden])")
            assert page.evaluate(fits), view
            shot(page, f"{view}-390x844.png", full_page=True)
        assert errors == []
    finally:
        context.close()
