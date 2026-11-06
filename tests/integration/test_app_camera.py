"""The camera tab and the face box, in a browser, because that is where they run.

1. Every row of the framing table, fed made-up boxes through the real
   web/app/framing.js. No camera, no detector, no video.
2. The sharpness threshold, measured on the test photo and on a blurred copy
   of it, through the same framing.sample() the camera loop uses.
3. A capture, end to end, with Chromium's fake camera playing a y4m made from
   the test photo. It reaches the countdown on its own, the preview goes green,
   and the photo lands in the thumb box.
4. The two ways it degrades: no face detector, and no camera at all.
5. The face box. Every photo, from either tab, is measured in the browser, and
   the box goes to POST /cards as the `face` field: null when there is no
   face, and no field at all when there is no detector to ask.

The y4m is made at run time by ffmpeg and thrown away. It is 4 MB and it comes
from a file already in the repo, so it does not belong in git.

Skips itself when the stack is not up. Start it with: make up-mock
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
from collections.abc import Iterator
from email.parser import BytesParser
from email.policy import HTTP
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image, ImageFilter

from api.qa import FACE_SMALL, NO_FACE, TIGHT
from card.render import FIXTURE_FACE
from tests.conftest import REPO_ROOT

pytestmark = pytest.mark.integration

PORTRAIT = REPO_ROOT / "tests" / "fixtures" / "portrait.jpg"
SHOTS = REPO_ROOT / "out" / "ui"
TABLET = {"width": 1366, "height": 1024}

# The fake camera frame, cut out of the 1024x683 test photo at native scale so
# the face is never resampled. At this crop BlazeFace puts the face at 0.256 of
# the frame height, 0.001 off centre, 0.156 down from the top, with the eye line
# 1.5 degrees off level, and the face crop measures luma 130 and sharpness
# 2883. Every rule passes, which is what makes a capture with no click possible.
FRAME_CROP = (200, 0, 840, 480)

# Where BlazeFace put the face in that frame, as x, y, side. The sharpness test
# crops there directly, so it needs no detector.
FACE_BOX = (258, 75, 123)

# Hold 1.5 s, count 3 s, and the fake device only runs at 10 fps.
CAPTURE_MS = 40_000

# One card on the mock stack. A few seconds, with room.
CARD_MS = 60_000

MISSING = object()


def frame(blur: float = 0.0) -> bytes:
    image = Image.open(PORTRAIT).convert("RGB").crop(FRAME_CROP)
    if blur:
        image = image.filter(ImageFilter.GaussianBlur(blur))
    buffer = BytesIO()
    image.save(buffer, "JPEG", quality=95)
    return buffer.getvalue()


def data_url(jpeg: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()


@pytest.fixture(scope="module")
def fake_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One second of the test photo as a y4m. Chromium loops it."""
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is not installed, so there is no fake camera to play")
    folder = tmp_path_factory.mktemp("fake-camera")
    still = folder / "frame.jpg"
    still.write_bytes(frame())
    video = folder / "camera.y4m"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-i", str(still)]
        + ["-t", "1", "-r", "10", "-pix_fmt", "yuv420p", str(video)],
        check=True,
    )
    return video


@pytest.fixture(scope="module")
def playwright_instance() -> Iterator[object]:
    playwright_api = pytest.importorskip("playwright.sync_api")
    with playwright_api.sync_playwright() as playwright:
        yield playwright


@pytest.fixture(scope="module")
def browser(playwright_instance) -> Iterator[object]:
    """A plain browser. It has no camera, which one test needs."""
    instance = playwright_instance.chromium.launch()
    yield instance
    instance.close()


@pytest.fixture(scope="module")
def camera_browser(playwright_instance, fake_video: Path) -> Iterator[object]:
    """A browser whose only camera is the test photo."""
    instance = playwright_instance.chromium.launch(
        args=[
            "--use-fake-device-for-media-stream",
            "--use-fake-ui-for-media-stream",
            f"--use-file-for-fake-video-capture={fake_video}",
        ]
    )
    yield instance
    instance.close()


def sign_in(page, gateway: str, key: str) -> None:
    page.goto(f"{gateway}/", wait_until="domcontentloaded")
    page.wait_for_selector("#signin:not([hidden])")
    page.fill("#key-input", key)
    page.click("#signin-btn")
    page.wait_for_selector("#app:not([hidden])", timeout=20_000)


def video_runs(page) -> None:
    page.wait_for_function("document.getElementById('cam-video').videoWidth > 0", timeout=20_000)


# ------------------------------------------------------------------ rules --

FRAME = {"width": 640, "height": 480}


def face(centre: float = 0.5, size: float = 0.24, top: float = 0.15) -> dict:
    """A square face box in view coordinates, placed by fractions of the frame."""
    side = size * FRAME["height"]
    return {
        "x": centre * FRAME["width"] - side / 2,
        "y": top * FRAME["height"],
        "width": side,
        "height": side,
    }


def eyes(degrees: float) -> list[dict]:
    """Two eye points 40 px apart, that many degrees off level."""
    import math

    rise = math.tan(math.radians(degrees)) * 20.0
    return [{"x": 300, "y": 150 - rise}, {"x": 340, "y": 150 + rise}]


def sample(**overrides) -> dict:
    """A frame where everything passes, before the overrides break one thing."""
    base = {
        "frame": FRAME,
        "face": face(),
        "eyes": eyes(0),
        "luma": 130,
        "sharpness": 2000,
        "recent": [face(), face(), face()],
    }
    base.update(overrides)
    return base


# name, sample, the hint the person sees, the colour of the ring
CASES = [
    ("everything passes", sample(), "", "green"),
    ("face right of the middle", sample(face=face(centre=0.65)), "move left", "amber"),
    ("face left of the middle", sample(face=face(centre=0.35)), "move right", "amber"),
    ("face too small", sample(face=face(size=0.12)), "move closer", "amber"),
    ("face too big", sample(face=face(size=0.40)), "move back", "amber"),
    ("no headroom", sample(face=face(top=0.03)), "lower the camera", "amber"),
    ("head tilted 12 degrees", sample(eyes=eyes(12)), "level your head", "amber"),
    ("too dark", sample(luma=40), "more light", "amber"),
    ("too bright", sample(luma=230), "too bright", "amber"),
    ("a soft frame", sample(sharpness=20), "hold still", "amber"),
    ("a face that is moving", sample(recent=[face(), face(centre=0.56)]), "hold still", "amber"),
    ("nobody there", sample(face=None), "step into the frame", "red"),
    (
        "off to one side and too far away",
        sample(face=face(centre=0.75, size=0.12)),
        "move left",
        "red",
    ),
]

EVALUATE = """
async (cases) => {
  const framing = await import("/app/framing.js");
  return cases.map((one) => framing.evaluate(one));
}
"""


def test_every_row_of_the_framing_table(browser, gateway: str) -> None:
    """The rules, over made-up boxes. This needs no key: /app is open."""
    page = browser.new_page(viewport=TABLET)
    try:
        page.goto(f"{gateway}/", wait_until="domcontentloaded")
        results = page.evaluate(EVALUATE, [one for _, one, _, _ in CASES])
        for (name, _, hint, level), result in zip(CASES, results, strict=True):
            assert result["hint"] == hint, f"{name}: hint was {result['hint']!r}"
            assert result["level"] == level, f"{name}: ring was {result['level']}"
            assert result["ok"] is (hint == ""), name
    finally:
        page.close()


MIRROR = """
async () => {
  const framing = await import("/app/framing.js");
  const frame = { width: 640, height: 480 };
  const raw = { x: 40, y: 72, width: 115, height: 115 };
  return {
    box: framing.mirrorBox({ x: 10, y: 20, width: 100, height: 100 }, 640),
    point: framing.mirrorPoint({ x: 10, y: 20 }, 640),
    raw: framing.evaluate({ frame, face: raw }).hint,
    view: framing.evaluate({ frame, face: framing.mirrorBox(raw, 640) }).hint,
  };
}
"""


def test_the_preview_is_a_mirror_so_the_hints_are_too(browser, gateway: str) -> None:
    """A face on the left of the raw frame is on the right of what you see."""
    page = browser.new_page(viewport=TABLET)
    try:
        page.goto(f"{gateway}/", wait_until="domcontentloaded")
        result = page.evaluate(MIRROR)
        assert result["box"] == {"x": 530, "y": 20, "width": 100, "height": 100}
        assert result["point"]["x"] == 630
        # Telling someone in a mirror to move the way the sensor sees them is
        # the one mistake that cannot be worked around, so the two must differ.
        assert result["raw"] == "move right"
        assert result["view"] == "move left"
    finally:
        page.close()


SHARPNESS = """
async ([sharpUrl, blurredUrl, box]) => {
  const framing = await import("/app/framing.js");
  const measure = async (url) => {
    const image = new Image();
    image.src = url;
    await image.decode();
    const canvas = document.createElement("canvas");
    canvas.width = 128;
    canvas.height = 128;
    const context = canvas.getContext("2d", { willReadFrequently: true });
    const [x, y, side] = box;
    context.drawImage(image, x, y, side, side, 0, 0, 128, 128);
    return framing.sample(context.getImageData(0, 0, 128, 128));
  };
  return {
    sharp: await measure(sharpUrl),
    blurred: await measure(blurredUrl),
    threshold: framing.SHARP_MIN,
    luma: framing.LUMA,
  };
}
"""


def test_a_blurred_frame_is_called_soft(browser, gateway: str) -> None:
    """Both ends of the threshold in framing.js, on the photo it was set from."""
    page = browser.new_page(viewport=TABLET)
    try:
        page.goto(f"{gateway}/", wait_until="domcontentloaded")
        measured = page.evaluate(
            SHARPNESS, [data_url(frame()), data_url(frame(blur=2)), list(FACE_BOX)]
        )
        threshold = measured["threshold"]
        assert measured["sharp"]["sharpness"] > threshold * 5
        assert measured["blurred"]["sharpness"] < threshold / 5
        # And the test photo is lit well enough to pass the light rule, which is
        # what lets the capture test run with no one at the camera.
        low, high = measured["luma"]
        assert low < measured["sharp"]["luma"] < high
    finally:
        page.close()


# ---------------------------------------------------------------- capture --

# True once the stage's frame has turned green: a 6 px inset shadow in the
# green token. Read from the computed style, so it is the pixels, not a class.
FRAME_IS_GREEN = """
() => {
  const stage = document.getElementById("cam-stage");
  const green = getComputedStyle(document.documentElement).getPropertyValue("--ak-green").trim();
  const probe = document.createElement("span");
  probe.style.color = green;
  document.body.append(probe);
  const want = getComputedStyle(probe).color;
  probe.remove();
  const shadow = getComputedStyle(stage, "::after").boxShadow;
  return shadow.includes(want) && shadow.includes("inset") && shadow.includes("6px");
}
"""


def test_the_camera_reaches_a_capture_on_its_own(
    camera_browser, gateway: str, live_key: str
) -> None:
    """Fake camera in, countdown, photo in the thumb box. Nothing is clicked but the tab."""
    page = camera_browser.new_page(viewport=TABLET)
    crashes: list[str] = []
    page.on("pageerror", lambda error: crashes.append(str(error)))
    try:
        sign_in(page, gateway, live_key)
        page.click('[data-tab="camera"]')
        page.wait_for_selector("#cam:not([hidden])")
        video_runs(page)
        assert page.evaluate("document.getElementById('cam-video').videoWidth") == 640

        # The detector loaded out of /vendor, so there is a guide.
        page.wait_for_function(
            "document.getElementById('cam-note').textContent.includes('BlazeFace')",
            timeout=30_000,
        )

        # It finds the face, everything passes, and it starts counting by itself.
        page.wait_for_selector("#cam-count:not([hidden])", timeout=CAPTURE_MS)
        assert page.get_attribute("#cam-ring", "data-level") == "green"
        assert page.get_attribute("#cam-face", "hidden") is None
        # The whole preview is framed in the ring's colour, the stage's ::after.
        assert page.get_attribute("#cam-stage", "data-level") == "green"
        page.wait_for_function(FRAME_IS_GREEN, timeout=5_000)
        SHOTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SHOTS / "camera-1366x1024.png"))

        # And the photo lands where an uploaded one would, measured: the frame
        # has a face, so there is no note under its size.
        page.wait_for_selector("#thumb-row:not([hidden])", timeout=CAPTURE_MS)
        assert page.inner_text("#thumb-size").startswith("640 x 480")
        assert page.inner_text("#thumb-name").startswith("camera-")
        assert page.locator("#thumb-note").is_hidden()

        # A photo releases the camera. A lamp left on is a bug people notice.
        page.wait_for_function(
            "document.getElementById('cam-video').srcObject === null", timeout=10_000
        )

        # Choose another gives it back.
        page.click("#retake-btn")
        video_runs(page)
        assert crashes == []
    finally:
        page.close()


def test_the_camera_stays_off_behind_the_card(
    camera_browser, gateway: str, live_key: str, mock_backends
) -> None:
    """The shutter, Generate, Make another: the camera stays off while the card
    is on show and while the photo is kept, and comes back with Choose another."""
    page = camera_browser.new_page(viewport=TABLET)
    try:
        sign_in(page, gateway, live_key)
        page.click('[data-tab="camera"]')
        video_runs(page)
        page.click("#cam-shutter")
        page.wait_for_selector("#thumb-row:not([hidden])", timeout=20_000)
        page.fill("#f-name", "Sam Rivera")
        page.click("#generate-btn")
        page.wait_for_selector("#result:not([hidden])", timeout=20_000)
        off = "document.getElementById('cam-video').srcObject === null"
        assert page.evaluate(off)

        page.wait_for_function(
            "document.getElementById('result-status').textContent.startsWith('Made in')",
            timeout=CARD_MS,
        )
        page.click("#result-again")
        page.wait_for_selector("#make-form:not([hidden])")
        assert page.evaluate(off)
        page.click("#retake-btn")
        video_runs(page)
    finally:
        page.close()


def test_the_camera_still_works_without_the_detector(
    camera_browser, gateway: str, live_key: str
) -> None:
    """web/vendor is gitignored. An api run from a fresh clone has no detector:
    the camera keeps its preview and its shutter, and the guide is off."""
    page = camera_browser.new_page(viewport=TABLET)
    try:
        page.route("**/vendor/**", lambda route: route.abort())
        sign_in(page, gateway, live_key)
        page.click('[data-tab="camera"]')
        video_runs(page)
        page.wait_for_function(
            "document.getElementById('cam-note').textContent.includes('guide is off')",
            timeout=30_000,
        )
        assert page.get_attribute("#cam-ring", "data-level") == "idle"
        # The loop paints the hint on its first frame, 66 ms after it starts.
        page.wait_for_function(
            "document.getElementById('cam-hint').textContent.includes('shutter')",
            timeout=10_000,
        )

        # The shutter is the whole fallback, so it had better work.
        page.click("#cam-shutter")
        page.wait_for_selector("#thumb-row:not([hidden])", timeout=20_000)
        assert page.inner_text("#thumb-size").startswith("640 x 480")
        assert "did not load" in page.inner_text("#thumb-note")
    finally:
        page.close()


def test_no_camera_says_so_and_points_at_upload(browser, gateway: str, live_key: str) -> None:
    """This browser has no camera. The tab has to say that instead of hanging."""
    context = browser.new_context(viewport=TABLET, permissions=["camera"])
    page = context.new_page()
    try:
        sign_in(page, gateway, live_key)
        page.click('[data-tab="camera"]')
        page.wait_for_selector("#cam-blocked:not([hidden])", timeout=20_000)
        assert page.locator("#cam").is_hidden()
        assert page.inner_text("#cam-blocked-lead").strip() != ""
        page.click("#cam-use-upload")
        page.wait_for_selector("#tab-upload:not([hidden])")
        assert page.evaluate("document.activeElement.dataset.tab") == "upload"
    finally:
        context.close()


# --------------------------------------------------------------- face box --


def form_field(request, name: str) -> object:
    """One field of a multipart POST, parsed as JSON. MISSING when it was not sent."""
    head = f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode()
    message = BytesParser(policy=HTTP).parsebytes(head + (request.post_data_buffer or b""))
    for part in message.iter_parts():
        if part.get_param("name", header="content-disposition") == name:
            return json.loads(part.get_content())
    return MISSING


def overlap(box: dict, other: tuple[int, int, int, int]) -> float:
    """Intersection over union of a posted box and an (x, y, width, height)."""
    x, y, width, height = other
    across = min(box["x"] + box["width"], x + width) - max(box["x"], x)
    down = min(box["y"] + box["height"], y + height) - max(box["y"], y)
    both = max(0, across) * max(0, down)
    return both / (box["width"] * box["height"] + width * height - both)


def post_photo(page, photo: Path) -> object:
    """Upload a photo, give a name, press Generate. Returns the posted `face`.

    Read off the wire by a route handler. A request event would not do:
    Chromium does not hand over a body with a Blob in it, and the photo is one.
    """
    posted: list[object] = []

    def record(route) -> None:
        if route.request.method == "POST":
            posted.append(form_field(route.request, "face"))
        route.continue_()

    page.route("**/cards", record)
    page.fill("#f-name", "Sam Rivera")
    page.set_input_files("#file-input", str(photo))
    page.wait_for_selector("#thumb-row:not([hidden])", timeout=30_000)
    page.click("#generate-btn")
    page.wait_for_selector("#result:not([hidden])", timeout=20_000)
    assert len(posted) == 1, posted
    return posted[0]


def warnings_when_made(page) -> list[str]:
    """The photo checks' warnings beside the card once it is made."""
    page.wait_for_function(
        "document.getElementById('result-status').textContent.startsWith('Made in')",
        timeout=CARD_MS,
    )
    return page.locator("#result-warnings .warning").all_inner_texts()


def whole_box(found: object, width: int, height: int) -> None:
    """Four whole numbers, inside the photo, as the api insists."""
    assert isinstance(found, dict), found
    assert sorted(found) == ["height", "width", "x", "y"]
    assert all(isinstance(value, int) for value in found.values()), found
    assert found["x"] >= 0 and found["y"] >= 0
    assert found["x"] + found["width"] <= width and found["y"] + found["height"] <= height
    assert min(found["width"], found["height"]) >= 16


CLAMP = """
async (cases) => {
  const photo = await import("/app/photo.js");
  return cases.map(([box, width, height]) =>
    photo.clampFace(box ? { box, score: 0.9 } : null, width, height));
}
"""

# The detector's box, the photo's size, and what may be posted: four whole
# numbers inside the photo, or null. The api answers 400 for a box one pixel
# past the edge, and a 400 is a card that never gets made.
CLAMP_CASES = [
    ("nothing found", None, 640, 480, None),
    (
        "inside, edges rounded",
        {"x": 10.4, "y": 20.6, "width": 100.2, "height": 99.9},
        640,
        480,
        {"x": 10, "y": 21, "width": 101, "height": 100},
    ),
    (
        "past the top and the left",
        {"x": -12.3, "y": -40.2, "width": 130, "height": 130},
        640,
        480,
        {"x": 0, "y": 0, "width": 118, "height": 90},
    ),
    (
        "past the bottom and the right",
        {"x": 600.2, "y": 450.7, "width": 80, "height": 80},
        640,
        480,
        {"x": 600, "y": 451, "width": 40, "height": 29},
    ),
    (
        "ends on the right edge after rounding",
        {"x": 539.6, "y": 0, "width": 100.4, "height": 100},
        640,
        480,
        {"x": 540, "y": 0, "width": 100, "height": 100},
    ),
    ("a sliver at the edge", {"x": 630, "y": 100, "width": 40, "height": 40}, 640, 480, None),
    ("under 16 px", {"x": 10, "y": 10, "width": 15.4, "height": 40}, 640, 480, None),
    ("not a number", {"x": float("nan"), "y": 0, "width": 50, "height": 50}, 640, 480, None),
]


def test_the_box_sent_is_whole_pixels_inside_the_photo(browser, gateway: str) -> None:
    """photo.clampFace over made-up boxes. /app is open, so no key."""
    page = browser.new_page(viewport=TABLET)
    try:
        page.goto(f"{gateway}/", wait_until="domcontentloaded")
        results = page.evaluate(CLAMP, [[box, w, h] for _, box, w, h, _ in CLAMP_CASES])
        for (name, _, width, height, want), got in zip(CLAMP_CASES, results, strict=True):
            assert got == want, f"{name}: {got}"
            if got is not None:
                whole_box(got, width, height)
    finally:
        page.close()


SURE = """
async (url) => {
  const detector = await import("/app/detector.js");
  const image = new Image();
  image.src = url;
  await image.decode();
  const canvas = document.createElement("canvas");
  canvas.width = image.naturalWidth;
  canvas.height = image.naturalHeight;
  canvas.getContext("2d").drawImage(image, 0, 0);
  const each = [];
  for (const pass of detector.PASSES) each.push(await detector.scan(canvas, pass));
  return { each, chosen: await detector.detectStill(canvas) };
}
"""


def test_the_surest_pass_wins(browser, gateway: str) -> None:
    """On the test photo all three passes find the face, with different boxes.
    The top score wins."""
    page = browser.new_page(viewport=TABLET)
    try:
        page.goto(f"{gateway}/", wait_until="domcontentloaded")
        result = page.evaluate(SURE, data_url(PORTRAIT.read_bytes()))
        each = result["each"]
        assert all(each), each
        assert result["chosen"] == max(each, key=lambda one: one["score"])
        # Not vacuous: the whole photo is the least sure of the three, so a
        # first-found or last-found rule would pick a different box.
        assert result["chosen"] != each[0]
    finally:
        page.close()


def test_the_detector_loads_at_sign_in_and_a_photo_waits_for_it(
    browser, gateway: str, live_key: str
) -> None:
    """12 MB of wasm starts to load at sign-in, not when the first photo arrives.

    The request is held here to stand in for a slow link. A photo chosen while
    it is held says so in the drop zone, and lands once the wasm arrives.
    """
    page = browser.new_page(viewport=TABLET)
    held: list[object] = []
    try:
        page.route("**/vendor/tasks-vision/wasm/*.wasm", lambda route: held.append(route))
        sign_in(page, gateway, live_key)
        for _ in range(100):
            if held:
                break
            page.wait_for_timeout(100)
        assert held, "the detector did not start loading at sign-in"

        page.set_input_files("#file-input", str(PORTRAIT))
        page.wait_for_function(
            "document.querySelector('#dropzone .dz-lead').textContent === 'Finding the face'"
        )
        assert page.get_attribute("#dropzone", "aria-busy") == "true"
        assert page.locator("#thumb-row").is_hidden()

        for route in held:
            route.continue_()
        page.wait_for_selector("#thumb-row:not([hidden])", timeout=30_000)
        assert page.locator("#thumb-note").is_hidden()
        assert page.get_attribute("#dropzone", "aria-busy") is None
    finally:
        page.close()


def test_an_upload_sends_the_face_it_found(
    browser, gateway: str, live_key: str, mock_backends
) -> None:
    """The test photo: a box on the face goes with it, and the card has no face warning."""
    page = browser.new_page(viewport=TABLET)
    try:
        sign_in(page, gateway, live_key)
        found = post_photo(page, PORTRAIT)
        whole_box(found, 1024, 683)
        # On the face, not only somewhere near it.
        assert overlap(found, FIXTURE_FACE) > 0.6, found
        warnings = warnings_when_made(page)
        assert NO_FACE not in warnings
        assert FACE_SMALL not in warnings
    finally:
        page.close()


def test_no_face_sends_null_and_the_card_says_so(
    browser, gateway: str, live_key: str, mock_backends, tmp_path: Path
) -> None:
    """A flat grey photo: the page says no face, posts null, and the card still comes out."""
    grey = tmp_path / "grey.jpg"
    Image.new("RGB", (1200, 900), (128, 128, 128)).save(grey, quality=90)
    page = browser.new_page(viewport=TABLET)
    try:
        sign_in(page, gateway, live_key)
        found = post_photo(page, grey)
        assert page.inner_text("#thumb-note").startswith("No face found.")
        # JSON null, not an empty field. The api reads an empty one as not
        # sent, and then the card could not say that no face was found.
        assert found is None
        assert NO_FACE in warnings_when_made(page)
    finally:
        page.close()


def test_without_the_detector_no_face_field_is_sent(
    browser, gateway: str, live_key: str, mock_backends
) -> None:
    """No detector, so the browser did not look, and says nothing about a face.
    The card is placed by the top of the outline and has no face warning."""
    page = browser.new_page(viewport=TABLET)
    try:
        page.route("**/vendor/**", lambda route: route.abort())
        sign_in(page, gateway, live_key)
        assert post_photo(page, PORTRAIT) is MISSING
        assert NO_FACE not in warnings_when_made(page)
    finally:
        page.close()


def test_a_tight_photo_says_so_beside_the_card(
    browser, gateway: str, live_key: str, mock_backends, tmp_path: Path
) -> None:
    """The test photo cut off at the chest. To stand on the plate the face grows
    past what the card wants, and the warning beside the card says to shoot
    from the waist up."""
    tight = tmp_path / "tight.jpg"
    Image.open(PORTRAIT).convert("RGB").crop((250, 0, 750, 450)).save(tight, quality=92)
    page = browser.new_page(viewport=TABLET)
    try:
        sign_in(page, gateway, live_key)
        assert post_photo(page, tight) is not None, "the face was found"
        assert TIGHT in warnings_when_made(page)
        assert page.is_visible("#result-warnings")
    finally:
        page.close()
