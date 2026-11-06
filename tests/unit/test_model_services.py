"""The mock model services, called in process.

Each one is the same program: bytes in, one model, bytes out, and the four
headers the api copies into the ledger. The gateway version of these checks is
in tests/security/test_gateway_auth.py. The mock of vLLM's routes are in
tests/unit/test_llm_mock.py; here is what it writes, held to the checks a real
back must pass.
"""

from __future__ import annotations

import importlib
import io
import json
import random

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from api.text import (
    ATTACK_BANDS,
    LAUNCH_BANDS,
    LEFT,
    PRAISE,
    RIGHT,
    UNSAID,
    build_messages,
    chat_payload,
    check,
)
from api.writing import BANNED, EM_DASH, phrases_in
from common.meta import ART_SOURCE, INFER_MS, LICENSE, MODEL, RAN_ON
from common.schemas import METRICS, CardText, Stats
from tests.conftest import TEST_KEY

AUTH = {"Authorization": f"Bearer {TEST_KEY}"}
WRONG = {"Authorization": "Bearer " + "w" * 64}


def reloaded(module_name: str) -> TestClient:
    """Re-import a service so it picks up the settings this test just set."""
    module = importlib.import_module(module_name)
    importlib.reload(module)
    return TestClient(module.app)


@pytest.fixture
def cutout(clean_settings) -> TestClient:
    with reloaded("cutout.main") as client:
        yield client


@pytest.fixture
def art(clean_settings) -> TestClient:
    with reloaded("art.main") as client:
        yield client


@pytest.fixture
def llm(clean_settings) -> TestClient:
    with reloaded("llm_mock.main") as client:
        yield client


def photo_bytes(size: tuple[int, int] = (300, 400)) -> bytes:
    """A dark blob on a light wall, which is the input this demo asks for."""
    image = Image.new("RGB", size, (210, 208, 205))
    image.paste(Image.new("RGB", (size[0] // 2, size[1] // 2), (40, 40, 45)), (size[0] // 4, 40))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG")
    return buffer.getvalue()


# -- cutout ------------------------------------------------------------------


def test_cutout_livez_is_the_only_open_route(cutout: TestClient) -> None:
    assert cutout.get("/livez").status_code == 200
    assert cutout.get("/healthz").status_code == 401
    assert cutout.post("/cutout").status_code == 401


def test_cutout_rejects_a_wrong_key(cutout: TestClient) -> None:
    assert cutout.get("/healthz", headers=WRONG).status_code == 401


def test_cutout_reports_the_mock_model_not_birefnet(cutout: TestClient) -> None:
    """A mock card must not claim BiRefNet made it."""
    body = cutout.get("/healthz", headers=AUTH).json()
    assert body["model"] == "mock/plain-wall-matte"
    assert body["loaded"] is True
    assert body["backend"] == "mock"


def test_cutout_returns_rgba_with_the_ledger_headers(cutout: TestClient) -> None:
    response = cutout.post("/cutout", headers=AUTH, files={"image": ("p.jpg", photo_bytes())})
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    with Image.open(io.BytesIO(response.content)) as image:
        assert image.mode == "RGBA"
        assert image.getchannel("A").getbbox() is not None
    assert response.headers[MODEL] == "mock/plain-wall-matte"
    assert response.headers[LICENSE] == "n/a"
    assert response.headers[RAN_ON] == "mock"
    assert int(response.headers[INFER_MS]) >= 0


def test_cutout_refuses_something_that_is_not_an_image(cutout: TestClient) -> None:
    response = cutout.post("/cutout", headers=AUTH, files={"image": ("s.json", b'{"name":"x"}')})
    assert response.status_code == 400


# -- art ---------------------------------------------------------------------


def test_art_livez_is_the_only_open_route(art: TestClient) -> None:
    assert art.get("/livez").status_code == 200
    assert art.get("/healthz").status_code == 401
    assert art.post("/art/generate", json={"prompt": "x"}).status_code == 401


def test_art_reports_the_mock_model_not_flux(art: TestClient) -> None:
    body = art.get("/healthz", headers=AUTH).json()
    assert body["model"] == "mock/procedural-stadium"
    assert body["loaded"] is True


def test_art_returns_a_png_at_the_size_it_was_asked_for(art: TestClient) -> None:
    request = {"prompt": "a stadium", "style": "classic", "width": 512, "height": 704, "seed": 3}
    response = art.post("/art/generate", headers=AUTH, json=request)
    assert response.status_code == 200
    with Image.open(io.BytesIO(response.content)) as image:
        assert image.size == (512, 704)
    assert response.headers[MODEL] == "mock/procedural-stadium"
    assert response.headers[LICENSE] == "n/a"
    assert response.headers[RAN_ON] == "mock"
    # Painted for this request: clean_settings turns the pool off.
    assert response.headers[ART_SOURCE] == "fresh"


def test_art_is_deterministic_in_the_seed(art: TestClient) -> None:
    request = {"prompt": "a stadium", "style": "akamai", "width": 256, "height": 352}
    first = art.post("/art/generate", headers=AUTH, json={**request, "seed": 11}).content
    again = art.post("/art/generate", headers=AUTH, json={**request, "seed": 11}).content
    other = art.post("/art/generate", headers=AUTH, json={**request, "seed": 12}).content
    assert first == again
    assert first != other


def test_art_caps_the_size_so_one_request_cannot_fill_the_card(art: TestClient) -> None:
    request = {"prompt": "x", "width": 4096, "height": 4096}
    assert art.post("/art/generate", headers=AUTH, json=request).status_code == 422


def test_art_needs_a_prompt(art: TestClient) -> None:
    assert art.post("/art/generate", headers=AUTH, json={"prompt": ""}).status_code == 422


# -- what the mock of vLLM writes -------------------------------------------


def test_llm_reads_the_players_facts_and_not_the_examples(llm: TestClient) -> None:
    """The system prompt names two example players. The back is about the third."""
    stats = Stats(
        name="Ka'Ni Quillfeather",
        position="DH",
        metrics={"swing_length_ft": 7.6, "launch_angle_deg": 16.0, "exit_velo_mph": 92.1},
    )
    request = chat_payload("m", build_messages(stats))
    body = llm.post("/v1/chat/completions", headers=AUTH, json=request).json()
    written = json.loads(body["choices"][0]["message"]["content"])
    assert "Rivera" not in json.dumps(written)
    assert "Okafor" not in json.dumps(written)
    assert "7.6" in written["blurb"]
    assert "16" in written["blurb"]
    assert check(CardText(**written), stats) == []


# Hometowns that have tripped, or could trip, a rule meant for something else.
HOMETOWNS = [
    None,
    None,
    "Chicago, IL",
    "St. Louis, MO",
    "Washington, D.C.",
    "College Park, MD",
    "Flat Rock, MI",
    "Level Green, PA",
    "Elite, TX",
    "29 Palms, CA",
    "San José, CA",
    "Coeur d'Alene, ID",
    "Llanfairpwllgwyngyll Gogerychwyrndrobwll Village, Wales UK",
]


def test_every_mock_back_passes_the_checks_a_real_one_must(clean_settings) -> None:
    """So the mock stack makes cards whose backs would pass on the node. 2,000
    players: names that share a word with a nickname on the list, apostrophes,
    accents, one metric, none, whole numbers, negative angles, and bios:
    whole-foot heights, hometowns with a stop, a band's name, a number or a
    word the facts never say, and positions that are words too."""
    mock = importlib.reload(importlib.import_module("llm_mock.main"))
    rng = random.Random(18)
    names = [
        "Sam Rivera",
        "Ka'Ni Quillfeather",
        "Rory O'Hara",
        "Inés Ortega",
        "Mary-Kate Green",
        "Bo",
        "Rocket Change",
        "Low Latency",
        "Theo The Great",
        "Ice Cold Start Turnstile",
    ]
    # Each metric's whole range, from the card's own table of them.
    spans = {key: (metric.low, metric.high, metric.decimals) for key, metric in METRICS.items()}
    for trial in range(2000):
        metrics = {}
        for key, (low, high, places) in spans.items():
            if rng.random() < 0.6:
                value = round(rng.uniform(low, high), places)
                metrics[key] = float(round(value)) if rng.random() < 0.2 else value
        stats = Stats(
            name=names[trial % len(names)],
            position=rng.choice([None, "SS", "C", "DH", "1B", "OF", "IF", "P"]),
            bats=rng.choice([None, "R", "L", "S"]),
            throws=rng.choice([None, "R", "L"]),
            height_in=rng.choice([None, None, 72, 60, rng.randint(36, 96)]),
            weight_lb=rng.choice([None, None, rng.randint(60, 500)]),
            hometown=rng.choice(HOMETOWNS),
            metrics=metrics,
        )
        written = CardText(**mock.card_text(build_messages(stats)[1]["content"]))
        assert check(written, stats) == [], (stats, written)


def test_the_mock_reads_the_height_and_the_weight_as_the_players(clean_settings) -> None:
    """They start with a number, like a measurement, and are not one."""
    mock = importlib.reload(importlib.import_module("llm_mock.main"))
    stats = Stats(name="Riley Chen", height_in=70, weight_lb=175, metrics={"exit_velo_mph": 93.0})
    player, measured = mock.read_facts(build_messages(stats)[1]["content"])
    assert measured == [("Exit Velocity", "93.0")]
    assert (player["Height"], player["Weight"]) == ("5 ft 10 in", "175 lb")


def test_a_one_number_mock_back_with_a_bio_says_one_bio_fact(clean_settings) -> None:
    """Three sentences, as the plan allows: the number, one bio fact as the
    facts write it, the verdict. With no bio, two, as before."""
    mock = importlib.reload(importlib.import_module("llm_mock.main"))
    bare = Stats(name="Riley Chen", bats="L", metrics={"exit_velo_mph": 93.0})
    told = bare.model_copy(update={"height_in": 70, "weight_lb": 175, "hometown": "Oakland, CA"})
    alone = mock.card_text(build_messages(bare)[1]["content"])["blurb"]
    with_bio = mock.card_text(build_messages(told)[1]["content"])["blurb"]
    assert len(alone.split(". ")) == 2
    assert len(with_bio.split(". ")) == 3
    said = with_bio.split(". ")[1] + "."
    assert said in {
        "Riley stands 5 ft 10 in.",
        "Riley weighs 175 lb.",
        "Riley calls Oakland, CA home.",
    }
    assert check(CardText(**mock.card_text(build_messages(told)[1]["content"])), told) == []


def test_the_mock_word_lists_hold_no_word_a_card_back_may_not_use(clean_settings) -> None:
    mock = importlib.reload(importlib.import_module("llm_mock.main"))
    entries = [
        *mock.NICKNAMES,
        *mock.HABITS,
        *mock.VERDICTS,
        *(frame for frames in mock.SENTENCES.values() for frame in frames),
        *mock.BIO.values(),
    ]
    # The mock does not read the angles or the sides, so it may not name them.
    bands = tuple(name for band in LAUNCH_BANDS + ATTACK_BANDS for name in band[3])
    for entry in entries:
        assert phrases_in(entry, BANNED + PRAISE + UNSAID) == [], entry
        assert phrases_in(entry, LEFT + RIGHT + bands) == [], entry
        assert EM_DASH not in entry, entry
    for verdict in mock.VERDICTS:
        assert len(verdict.split()) <= 5, verdict


def test_every_service_that_takes_a_form_declares_the_parser() -> None:
    """FastAPI needs python-multipart to read File and Form fields, and raises at
    import without it. The laptop's venv always has it, because the api needs
    it, so only a check like this sees a service image built without it."""
    import re
    import tomllib

    from tests.conftest import REPO_ROOT

    for main in sorted((REPO_ROOT / "services").glob("*/src/*/main.py")):
        source = main.read_text()
        takes_a_form = re.search(r"from fastapi import [^\n]*\b(File|Form|UploadFile)\b", source)
        if not takes_a_form:
            continue
        project = main.parents[2] / "pyproject.toml"
        dependencies = tomllib.loads(project.read_text())["project"]["dependencies"]
        assert any(dep.startswith("python-multipart") for dep in dependencies), (
            f"{main.relative_to(REPO_ROOT)} reads a form, and {project.relative_to(REPO_ROOT)} "
            "does not install python-multipart"
        )
