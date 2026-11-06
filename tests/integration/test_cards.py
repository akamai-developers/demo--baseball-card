"""A whole card, made the way a user makes one: through Caddy, with the key.

This is the only test that runs the real renderer through the stack, so it is
also the one that proves a card comes out at the size a card is meant to be.

Skips itself when the stack is not up. Start it with: make up-mock
"""

from __future__ import annotations

import io
import json
import subprocess
import time

import httpx
import pytest
from PIL import Image

from api.pipeline import STEPS
from api.text import check
from card import CARD_H, CARD_W
from card.render import FIXTURE_FACE
from common.schemas import CardText, Stats
from tests.conftest import REPO_ROOT

pytestmark = pytest.mark.integration

TIMEOUT = 120.0


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def create(
    gateway: str,
    key: str,
    photo: bytes,
    stats: str,
    face: str | None = None,
    style: str | None = None,
) -> httpx.Response:
    data = {"stats": stats}
    if face is not None:
        data["face"] = face
    if style is not None:
        data["style"] = style
    return httpx.post(
        f"{gateway}/cards",
        headers=auth(key),
        files={"photo": ("me.jpg", photo, "image/jpeg")},
        data=data,
        timeout=60,
    )


def get(gateway: str, key: str, path: str) -> httpx.Response:
    return httpx.get(f"{gateway}{path}", headers=auth(key), timeout=30)


def remove(gateway: str, key: str, card_id: str) -> httpx.Response:
    return httpx.delete(f"{gateway}/cards/{card_id}", headers=auth(key), timeout=30)


def finish(gateway: str, key: str, card_id: str) -> dict:
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        job = httpx.get(f"{gateway}/cards/{card_id}", headers=auth(key), timeout=30).json()
        if job["status"] in ("done", "failed"):
            return job
        time.sleep(0.5)
    raise AssertionError(f"card {card_id} was still {job['status']} after {TIMEOUT:.0f}s")


def make(request, stats: str, **fields) -> dict:
    gateway = request.getfixturevalue("gateway")
    key = request.getfixturevalue("live_key")
    photo = request.getfixturevalue("portrait").read_bytes()
    response = create(gateway, key, photo, stats, **fields)
    assert response.status_code == 202, response.text
    card_id = response.json()["card_id"]
    job = finish(gateway, key, card_id)
    return {"gateway": gateway, "key": key, "card_id": card_id, "job": job}


@pytest.fixture(scope="module")
def made(request) -> dict:
    """One card with every number, made once, read by several tests. Removed at the end."""
    card = make(request, request.getfixturevalue("example_stats"))
    yield card
    remove(card["gateway"], card["key"], card["card_id"])


@pytest.fixture(scope="module")
def plain(request) -> dict:
    """A card with a name and nothing else."""
    card = make(request, '{"name": "Sam Rivera"}', style="classic")
    yield card
    remove(card["gateway"], card["key"], card["card_id"])


def test_the_card_finishes(made: dict) -> None:
    assert made["job"]["status"] == "done", made["job"].get("error")


def test_every_step_is_in_the_ledger(made: dict) -> None:
    steps = {step["step"]: step for step in made["job"]["steps"]}
    # Read from the pipeline, so adding a step to it cannot leave this behind.
    assert set(steps) == set(STEPS)
    assert all(step["status"] == "done" for step in steps.values())


def test_the_model_steps_name_a_model_a_licence_and_where_they_ran(
    made: dict, mock_backends
) -> None:
    """These four fields are what the back of the card prints.

    The mocks are labelled by the card each stands in for on the default
    one-GPU node: GPU 0. Comps is numpy in the api, plain mock.
    """
    steps = {step["step"]: step for step in made["job"]["steps"]}
    where = {"cutout": "mock-gpu0", "art": "mock-gpu0", "comps": "mock", "text": "mock-gpu0"}
    for name, label in where.items():
        step = steps[name]
        assert step["model"], name
        assert step["license"], name
        assert step["ran_on"] == label, name
        assert isinstance(step["ms"], int), name
    assert steps["art"]["source"] == "fresh"


def test_the_mock_stack_does_not_claim_the_real_models_made_it(made: dict, mock_backends) -> None:
    models = {step["model"] for step in made["job"]["steps"] if step["model"]}
    assert not any("BiRefNet" in m or "FLUX" in m or "gemma" in m.lower() for m in models)


def test_the_text_came_out_and_passes_the_checks(made: dict, example_stats: str) -> None:
    """Through the whole stack, the back passes the checks the node's back has to."""
    text = CardText.model_validate(made["job"]["text"])
    assert text.nickname
    assert text.blurb
    assert check(text, Stats.model_validate_json(example_stats)) == []


def test_a_card_with_numbers_has_three_comps(made: dict) -> None:
    comps = made["job"]["comps"]
    assert len(comps) == 3
    assert all(comp["season"] == 2025 and 0 < comp["similarity"] <= 1 for comp in comps)


def test_a_card_with_no_numbers_has_no_comps(plain: dict) -> None:
    """No numbers, so no stats table and no "Swings like" on the back, and the
    ledger says why."""
    job = plain["job"]
    assert job["status"] == "done", job.get("error")
    assert job["comps"] == []
    comps = next(step for step in job["steps"] if step["step"] == "comps")
    assert (comps["status"], comps["error"]) == ("skipped", "no metrics were submitted")
    assert "no swing numbers" in job["text"]["blurb"]


def test_two_styles_make_two_different_fronts(made: dict, plain: dict) -> None:
    fronts = [
        get(card["gateway"], card["key"], f"/cards/{card['card_id']}/front.png").content
        for card in (made, plain)
    ]
    assert fronts[0] != fronts[1]


@pytest.mark.parametrize("name", ["front.png", "back.png"])
def test_both_sides_are_exactly_card_sized(made: dict, name: str) -> None:
    response = get(made["gateway"], made["key"], f"/cards/{made['card_id']}/{name}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    with Image.open(io.BytesIO(response.content)) as image:
        assert image.size == (CARD_W, CARD_H)


def test_the_preview_is_a_smaller_jpeg(made: dict) -> None:
    response = get(made["gateway"], made["key"], f"/cards/{made['card_id']}/preview.jpg")
    assert response.status_code == 200
    with Image.open(io.BytesIO(response.content)) as image:
        assert image.format == "JPEG"
        assert image.width < CARD_W


def test_a_card_is_not_cached_anywhere(made: dict) -> None:
    response = get(made["gateway"], made["key"], f"/cards/{made['card_id']}")
    assert response.headers["cache-control"] == "no-store"


def test_a_made_card_lists_the_files_it_has(made: dict) -> None:
    """GET /cards/{id} says which files are there. The front and its preview
    arrive a moment before the back, and a made card has all three."""
    assert made["job"]["assets"] == ["front.png", "back.png", "preview.jpg"]


# -- the things a caller gets wrong ------------------------------------------


def test_stats_with_no_name_is_a_400(gateway: str, live_key: str, portrait) -> None:
    response = create(gateway, live_key, portrait.read_bytes(), '{"number": 23}')
    assert response.status_code == 400
    assert "name" in response.json()["detail"]


def test_a_file_that_is_not_an_image_is_a_400(gateway: str, live_key: str) -> None:
    response = create(gateway, live_key, b'{"name": "wrong file"}', '{"name": "Sam"}')
    assert response.status_code == 400


def test_a_style_that_is_not_one_is_a_400(gateway: str, live_key: str, portrait) -> None:
    response = create(gateway, live_key, portrait.read_bytes(), '{"name": "Sam"}', style="neon")
    assert response.status_code == 400
    assert response.json()["detail"] == "style is not usable: use one of akamai, classic"


def test_a_photo_over_the_caddy_body_cap_does_not_reach_the_api(
    gateway: str, live_key: str
) -> None:
    """Caddy caps request bodies at 20 MB, so the api never decodes one."""
    response = create(gateway, live_key, b"\xff\xd8\xff" + b"x" * 25_000_000, '{"name": "Sam"}')
    assert response.status_code in (400, 413)


def test_an_unknown_card_is_a_404_not_a_500(gateway: str, live_key: str) -> None:
    assert get(gateway, live_key, "/cards/UnknownButWellFormed").status_code == 404


def on_the_volume(docker: list[str], card_id: str) -> list[str]:
    """Which of the card's three folders are on the api's data volume."""
    script = (
        "import pathlib, sys; "
        "print(' '.join(f for f in ('uploads', 'cards', 'work') "
        "if (pathlib.Path('/data') / f / sys.argv[1]).exists()))"
    )
    result = subprocess.run(
        [*docker, "compose", "exec", "-T", "api", "python", "-c", script, card_id],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.split()


def test_delete_removes_the_card_and_its_files(
    gateway: str, live_key: str, portrait, example_stats, docker
) -> None:
    """The rows, the photo, the intermediates and the card, read off the data
    volume itself and not only through the api."""
    card_id = create(gateway, live_key, portrait.read_bytes(), example_stats).json()["card_id"]
    finish(gateway, live_key, card_id)
    assert on_the_volume(docker, card_id) == ["uploads", "cards", "work"]

    assert remove(gateway, live_key, card_id).status_code == 204
    assert get(gateway, live_key, f"/cards/{card_id}").status_code == 404
    assert get(gateway, live_key, f"/cards/{card_id}/front.png").status_code == 404
    assert remove(gateway, live_key, card_id).status_code == 404
    assert on_the_volume(docker, card_id) == []


# -- the face box ------------------------------------------------------------
#
# The browser measures the face and posts the box with the photo. FIXTURE_FACE is
# the fixture portrait's face as the browser's detector found it, in the
# photo's pixels.

FACE_WARNINGS = {
    "no face found",
    "placed low",
    "placed high",
    "face small",
    "face large",
    "floats",
    "tight photo, shoot from the waist up or lower",
}


def test_a_face_box_on_the_person_says_nothing(gateway: str, live_key: str, portrait) -> None:
    face = json.dumps(dict(zip(("x", "y", "width", "height"), FIXTURE_FACE, strict=True)))
    response = create(gateway, live_key, portrait.read_bytes(), '{"name": "Sam Rivera"}', face=face)
    assert response.status_code == 202, response.text
    card_id = response.json()["card_id"]
    try:
        job = finish(gateway, live_key, card_id)
        assert job["status"] == "done", job.get("error")
        assert not FACE_WARNINGS & set(job["warnings"]), job["warnings"]
    finally:
        remove(gateway, live_key, card_id)


def test_a_null_face_says_so(gateway: str, live_key: str, portrait) -> None:
    response = create(gateway, live_key, portrait.read_bytes(), '{"name": "Sam"}', face="null")
    assert response.status_code == 202, response.text
    card_id = response.json()["card_id"]
    try:
        job = finish(gateway, live_key, card_id)
        assert job["status"] == "done", job.get("error")
        assert "no face found" in job["warnings"]
    finally:
        remove(gateway, live_key, card_id)


def test_a_flat_grey_photo_with_no_face_says_both(
    gateway: str, live_key: str, mock_backends
) -> None:
    """What the app posts for a photo with nobody in it. The matte finds nobody,
    the browser found no face, and the card says both and still comes out."""
    buffer = io.BytesIO()
    Image.new("RGB", (1024, 768), (128, 128, 128)).save(buffer, "JPEG")
    response = create(gateway, live_key, buffer.getvalue(), '{"name": "Sam"}', face="null")
    assert response.status_code == 202, response.text
    card_id = response.json()["card_id"]
    try:
        job = finish(gateway, live_key, card_id)
        assert job["status"] == "done", job.get("error")
        assert job["warnings"] == ["no subject found", "no face found"]
    finally:
        remove(gateway, live_key, card_id)


def test_a_face_box_past_the_edge_of_the_photo_is_a_400(
    gateway: str, live_key: str, portrait
) -> None:
    """The portrait is 1024 px wide, and 1000 + 123 is past it."""
    face = '{"x": 1000, "y": 75, "width": 123, "height": 123}'
    response = create(gateway, live_key, portrait.read_bytes(), '{"name": "Sam"}', face=face)
    assert response.status_code == 400
    assert response.json()["detail"] == (
        "face is not usable: the box runs past the edge of the photo"
    )


# -- the node's other routes ---------------------------------------------------


def test_healthz_reports_all_three_model_services(gateway: str, live_key: str) -> None:
    body = get(gateway, live_key, "/healthz").json()
    assert set(body["services"]) == {"cutout", "art", "llm"}
    assert all(service["loaded"] for service in body["services"].values()), body["services"]
    assert body["ok"] is True


def test_the_models_are_reachable_directly_through_the_gateway(
    gateway: str, live_key: str, portrait
) -> None:
    """The node is not only a card maker. Each model is on its own path."""
    cut = httpx.post(
        f"{gateway}/cutout",
        headers=auth(live_key),
        files={"image": ("me.jpg", portrait.read_bytes(), "image/jpeg")},
        timeout=120,
    )
    assert cut.status_code == 200
    assert cut.headers["content-type"] == "image/png"

    art = httpx.post(
        f"{gateway}/art/generate",
        headers=auth(live_key),
        json={"prompt": "a stadium", "style": "classic", "width": 256, "height": 352},
        timeout=120,
    )
    assert art.status_code == 200
    assert art.headers["X-Art-Source"] == "fresh"

    chat = httpx.post(
        f"{gateway}/v1/chat/completions",
        headers=auth(live_key),
        json={"model": "mock/wordlist", "messages": [{"role": "user", "content": "hello"}]},
        timeout=60,
    )
    assert chat.status_code == 200
    assert json.loads(chat.text)["choices"][0]["message"]["content"]


def test_gpu_answers_with_the_mock_card(gateway: str, live_key: str, mock_backends) -> None:
    body = get(gateway, live_key, "/gpu").json()
    assert body["backend"] == "mock"
    assert [gpu["index"] for gpu in body["gpus"]] == [0]
