"""The card routes, called in process. The gateway version is in tests/integration."""

from __future__ import annotations

import io
import json
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from api import pipeline as pipeline_module
from api.pipeline import ASSET_NAMES
from tests.conftest import TEST_KEY

AUTH = {"Authorization": f"Bearer {TEST_KEY}"}


@pytest.fixture
def client(clean_settings, monkeypatch) -> TestClient:
    """The api with Chromium stubbed out. The real render runs in tests/integration."""

    def fake_render(stats, *, style, art, cutout, out_dir, text, comps, steps, names):
        out_dir.mkdir(parents=True, exist_ok=True)
        written = {}
        for side in ASSET_NAMES:
            path = out_dir / names[side]
            path.write_bytes(b"rendered")
            written[side] = path
        return written

    monkeypatch.setattr(pipeline_module, "render_card", fake_render)

    from api.main import app

    with TestClient(app) as test_client:
        yield test_client


def photo(size: tuple[int, int] = (400, 300)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (200, 200, 200)).save(buffer, "JPEG")
    return buffer.getvalue()


def post(
    client: TestClient,
    stats: str,
    image: bytes | None = None,
    face: str | None = None,
    style: str | None = None,
):
    data = {"stats": stats}
    if face is not None:
        data["face"] = face
    if style is not None:
        data["style"] = style
    return client.post(
        "/cards",
        headers=AUTH,
        files={"photo": ("me.jpg", image if image is not None else photo(), "image/jpeg")},
        data=data,
    )


def wait(client: TestClient, card_id: str, tries: int = 200) -> dict:
    import time

    for _ in range(tries):
        job = client.get(f"/cards/{card_id}", headers=AUTH).json()
        if job["status"] in ("done", "failed"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"card {card_id} never finished")


# -- create ------------------------------------------------------------------


def test_a_good_upload_is_accepted(client: TestClient, example_stats: str) -> None:
    response = post(client, example_stats)
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert response.headers["Location"] == f"/cards/{body['card_id']}"
    assert response.headers["Cache-Control"] == "no-store"


def test_only_a_name_is_required(client: TestClient) -> None:
    assert post(client, '{"name": "Sam"}').status_code == 202


def test_stats_without_a_name_is_a_400(client: TestClient) -> None:
    response = post(client, '{"number": 23}')
    assert response.status_code == 400
    assert "name" in response.json()["detail"]


def test_stats_that_is_not_json_is_a_400(client: TestClient) -> None:
    response = post(client, "{name: Sam}")
    assert response.status_code == 400
    assert "not valid JSON" in response.json()["detail"]


def test_an_unknown_stats_field_is_a_400(client: TestClient) -> None:
    """extra=forbid, so a typo is caught instead of quietly ignored."""
    response = post(client, '{"name": "Sam", "postion": "SS"}')
    assert response.status_code == 400


def test_a_field_the_card_does_not_know_is_refused_by_its_name(client: TestClient) -> None:
    response = post(client, '{"name": "Sam", "team": "Riverside Owls"}')
    assert response.status_code == 400
    assert response.json()["detail"].startswith("team is not part of a card. It takes name,")


TAKES = "It takes name, number, position, bats, throws, height_in, weight_lb, hometown, metrics."


@pytest.mark.parametrize(
    ("stats", "said"),
    [
        ({"number": 23}, "The card needs a name."),
        ({"name": "   "}, "The name is empty. The card needs one."),
        ({"name": "N" * 61}, "The name is 61 characters. It can be 60 at most."),
        ({"name": None}, "The name has to be text."),
        ({"name": "Sam", "number": 120}, "The number is 120. It has to be from 0 to 99."),
        ({"name": "Sam", "number": -1}, "The number is -1. It has to be from 0 to 99."),
        (
            {"name": "Sam", "number": 7.5},
            "The number is 7.5. It has to be a whole number from 0 to 99.",
        ),
        (
            {"name": "Sam", "number": "x"},
            'The number is "x". It has to be a whole number from 0 to 99.',
        ),
        ({"name": "Sam", "height_in": 30}, "Height is 30 in. It has to be from 36 to 96 in."),
        ({"name": "Sam", "weight_lb": 900}, "Weight is 900 lb. It has to be from 60 to 500 lb."),
        ({"name": "Sam", "bats": "X"}, 'Bats is "X". Use R, L or S.'),
        ({"name": "Sam", "throws": "S"}, 'Throws is "S". Use R or L.'),
        (
            {"name": "Sam", "position": "SSSSS"},
            "The position is 5 characters. It can be 4 at most.",
        ),
        (
            {"name": "Sam", "hometown": "H" * 61},
            "The hometown is 61 characters. It can be 60 at most.",
        ),
        (
            {"name": "Sam", "metrics": {"bat_speed_mph": 99}},
            "Bat Speed is 99 mph. It has to be from 50 to 85 mph.",
        ),
        (
            {"name": "Sam", "metrics": {"bat_speed_mph": "fast"}},
            'Bat Speed is "fast". It has to be a number from 50 to 85 mph.',
        ),
        (
            {"name": "Sam", "metrics": [72]},
            'metrics has to be an object of numbers, such as {"bat_speed_mph": 71.5}.',
        ),
        ({"name": "Sam", "jersey": 5}, f"jersey is not part of a card. {TAKES}"),
        ([1, 2], 'The stats have to be a JSON object, such as {"name": "Sam Rivera"}.'),
    ],
)
def test_stats_that_will_not_do_get_a_sentence_that_says_how_to_fix_them(
    client: TestClient, stats, said
) -> None:
    """The words the app shows under Generate, and cardctl prints. Each names the
    field as the form labels it, what was sent, and what it can be."""
    response = post(client, json.dumps(stats))
    assert (response.status_code, response.json()["detail"]) == (400, said)


def test_every_problem_in_the_stats_is_said_at_once(client: TestClient) -> None:
    response = post(client, json.dumps({"name": "Sam", "number": 120, "height_in": 30}))
    assert response.json()["detail"] == (
        "The number is 120. It has to be from 0 to 99. "
        "Height is 30 in. It has to be from 36 to 96 in."
    )


def test_a_post_without_its_photo_or_its_stats_says_which(client: TestClient) -> None:
    """FastAPI's own answer is a 422 and a list of dictionaries. This is a 400
    and the sentences, like every other form the api cannot use."""
    no_photo = client.post("/cards", headers=AUTH, data={"stats": '{"name": "Sam"}'})
    assert (no_photo.status_code, no_photo.json()["detail"]) == (
        400,
        "The photo is missing. Send it as a file in a form field named photo.",
    )
    no_stats = client.post("/cards", headers=AUTH, files={"photo": ("me.jpg", photo())})
    assert no_stats.json()["detail"] == (
        "The stats are missing. Send them as JSON in a form field named stats, "
        'such as {"name": "Sam Rivera"}.'
    )
    as_text = client.post("/cards", headers=AUTH, data={"stats": "{}", "photo": "me.jpg"})
    assert "The photo has to be a file, not text." in as_text.json()["detail"]


def test_the_style_is_its_own_field_and_not_part_of_the_stats(client: TestClient) -> None:
    response = post(client, '{"name": "Sam", "style": "classic"}')
    assert response.status_code == 400
    assert response.json()["detail"].startswith("style is not part of a card.")


@pytest.mark.parametrize("style", ["akamai", "classic"])
def test_each_style_is_accepted_and_kept(client: TestClient, style: str) -> None:
    card_id = post(client, '{"name": "Sam"}', style=style).json()["card_id"]
    with client.app.state.db.connect() as connection:
        row = connection.execute("SELECT style FROM cards WHERE card_id = ?", (card_id,)).fetchone()
    assert row["style"] == style


def test_no_style_is_the_first_one(client: TestClient) -> None:
    card_id = post(client, '{"name": "Sam"}').json()["card_id"]
    with client.app.state.db.connect() as connection:
        row = connection.execute("SELECT style FROM cards WHERE card_id = ?", (card_id,)).fetchone()
    assert row["style"] == "akamai"


@pytest.mark.parametrize("style", ["pastel", "Akamai", "summer"])
def test_a_style_that_is_not_one_is_a_400_and_makes_no_card(client: TestClient, style) -> None:
    before = client.app.state.db.count()
    response = post(client, '{"name": "Sam"}', style=style)
    assert response.status_code == 400
    assert response.json()["detail"] == f'There is no style "{style}". Use akamai or classic.'
    assert client.app.state.db.count() == before


def test_something_that_is_not_an_image_is_a_400(client: TestClient) -> None:
    response = post(client, '{"name": "Sam"}', image=b'{"oops": "the stats file"}')
    assert response.status_code == 400
    assert response.json()["detail"] == "That file is not a photo. Send a JPEG, PNG or WebP."


def test_an_oversize_photo_is_a_400(client: TestClient, clean_settings) -> None:
    too_big = b"\xff\xd8\xff" + b"x" * (clean_settings.max_upload_mb * 1_000_000)
    response = post(client, '{"name": "Sam"}', image=too_big)
    assert response.status_code == 400
    assert response.json()["detail"] == "The photo is over the 15 MB limit."


def test_the_stored_photo_is_downscaled(client: TestClient, clean_settings) -> None:
    body = post(client, '{"name": "Sam"}', image=photo((4000, 3000))).json()
    stored = clean_settings.data_dir / "uploads" / body["card_id"] / "photo.jpg"
    with Image.open(stored) as image:
        assert max(image.size) == clean_settings.max_photo_px


# -- the face box ------------------------------------------------------------


def face_file(settings, card_id: str):
    return settings.data_dir / "uploads" / card_id / "face.json"


def box(x: int, y: int, width: int, height: int) -> str:
    return json.dumps({"x": x, "y": y, "width": width, "height": height})


def test_a_face_box_is_stored_beside_the_photo(client: TestClient, clean_settings) -> None:
    body = post(client, '{"name": "Sam"}', face=box(150, 40, 100, 120)).json()
    stored = json.loads(face_file(clean_settings, body["card_id"]).read_text())
    assert stored == {"x": 150, "y": 40, "width": 100, "height": 120}
    assert wait(client, body["card_id"])["status"] == "done"


def test_a_face_box_is_scaled_with_the_photo(client: TestClient, clean_settings) -> None:
    """4000 x 3000 is stored at 2048 x 1536, 0.512 of it. So is the box."""
    response = post(
        client, '{"name": "Sam"}', image=photo((4000, 3000)), face=box(1000, 500, 400, 400)
    )
    assert response.status_code == 202, response.text
    stored = json.loads(face_file(clean_settings, response.json()["card_id"]).read_text())
    assert stored == {"x": 512, "y": 256, "width": 205, "height": 205}


def test_a_box_on_the_edge_of_a_scaled_photo_still_fits(client: TestClient, clean_settings) -> None:
    """Pillow rounds the short side of 3001 x 2001 up to 1366. The box that fills
    the photo the browser sent has to fill the stored one, not run past it."""
    response = post(
        client, '{"name": "Sam"}', image=photo((3001, 2001)), face=box(0, 0, 3001, 2001)
    )
    assert response.status_code == 202, response.text
    stored = json.loads(face_file(clean_settings, response.json()["card_id"]).read_text())
    assert stored == {"x": 0, "y": 0, "width": 2048, "height": 1366}


def test_a_null_face_is_stored_as_null(client: TestClient, clean_settings) -> None:
    """The browser looked and found no face."""
    body = post(client, '{"name": "Sam"}', face="null").json()
    assert face_file(clean_settings, body["card_id"]).read_text() == "null"


def test_no_face_field_leaves_no_face_file(client: TestClient, clean_settings) -> None:
    """What cardctl sends. No file, so no face warning either way."""
    body = post(client, '{"name": "Sam"}').json()
    assert (clean_settings.data_dir / "uploads" / body["card_id"] / "photo.jpg").is_file()
    assert not face_file(clean_settings, body["card_id"]).exists()


@pytest.mark.parametrize(
    ("face", "said"),
    [
        (box(350, 40, 100, 120), "runs past the edge of the photo"),  # 350 + 100 > 400
        (box(150, 200, 100, 120), "runs past the edge of the photo"),  # 200 + 120 > 300
        (box(150, 40, 15, 120), "a side is under 16 px"),
        (box(150, 40, 100, 0), "a side is under 16 px"),
        (box(-5, 40, 100, 120), "x: Input should be greater than or equal to 0"),
        ('{"x": 150, "y": 40, "width": 100}', "height: Field required"),
        ('{"x": 150, "y": 40, "width": 100, "height": 120, "score": 0.9}', "score"),
        ('{"x": "150", "y": 40, "width": 100, "height": 120}', "x: Input should be a valid"),
        ('{"x": 150.5, "y": 40, "width": 100, "height": 120}', "x: Input should be a valid"),
        ('{"x": true, "y": 40, "width": 100, "height": 120}', "x: Input should be a valid"),
        ("[150, 40, 100, 120]", "face: Input should be a valid dictionary"),
        (box(10**400, 40, 100, 120), "x: Input should be less than or equal to"),
    ],
)
def test_a_face_box_that_is_not_usable_is_a_400(
    client: TestClient, clean_settings, face: str, said: str
) -> None:
    response = post(client, '{"name": "Sam"}', face=face)
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail.startswith("face is not usable: ")
    assert said in detail
    # Nothing was written: the 400 comes before the card exists.
    assert not any((clean_settings.data_dir / "uploads").iterdir())


def test_a_side_under_16_px_after_scaling_is_a_400(client: TestClient) -> None:
    """30 px on a 4000 px photo is 15 px on the 2048 px one that is stored."""
    response = post(
        client, '{"name": "Sam"}', image=photo((4000, 3000)), face=box(1000, 500, 30, 30)
    )
    assert response.status_code == 400
    assert response.json()["detail"] == (
        "face is not usable: a side is under 16 px once the photo is scaled to 2048 x 1536"
    )


def test_a_face_field_that_is_not_json_is_a_400(client: TestClient) -> None:
    response = post(client, '{"name": "Sam"}', face="{x: 1}")
    assert response.status_code == 400
    assert response.json()["detail"].startswith("The face field is not valid JSON")


# -- read --------------------------------------------------------------------


def test_the_job_reaches_done_and_carries_the_ledger(client: TestClient, example_stats) -> None:
    card_id = post(client, example_stats).json()["card_id"]
    job = wait(client, card_id)
    assert job["status"] == "done"
    assert [step["step"] for step in job["steps"]] == list(pipeline_module.STEPS)
    assert job["text"]["blurb"]


def test_an_unknown_card_is_a_404(client: TestClient) -> None:
    assert client.get("/cards/UnknownButWellFormed", headers=AUTH).status_code == 404


def test_a_card_id_that_could_escape_the_data_directory_is_a_404(client: TestClient) -> None:
    assert client.get("/cards/..%2f..%2fetc", headers=AUTH).status_code == 404


# -- assets ------------------------------------------------------------------


def test_the_three_files_come_back(client: TestClient, example_stats: str) -> None:
    card_id = post(client, example_stats).json()["card_id"]
    wait(client, card_id)
    for name in ("front.png", "back.png", "preview.jpg"):
        response = client.get(f"/cards/{card_id}/{name}", headers=AUTH)
        assert response.status_code == 200, name
        assert response.headers["Cache-Control"] == "no-store"


def test_a_file_that_is_not_a_card_asset_is_a_404(client: TestClient, example_stats) -> None:
    card_id = post(client, example_stats).json()["card_id"]
    wait(client, card_id)
    response = client.get(f"/cards/{card_id}/cards.db", headers=AUTH)
    assert response.status_code == 404
    assert "front.png" in response.json()["detail"]


def test_asking_for_a_file_before_it_is_ready_says_so(client: TestClient) -> None:
    card_id = post(client, '{"name": "Sam"}').json()["card_id"]
    response = client.get(f"/cards/{card_id}/front.png", headers=AUTH)
    if response.status_code == 404:
        assert "not ready" in response.json()["detail"]
    else:
        assert response.status_code == 200  # the job can win the race, and that is fine


def test_a_made_card_lists_its_three_files(client: TestClient, example_stats: str) -> None:
    card_id = post(client, example_stats).json()["card_id"]
    job = wait(client, card_id)
    assert job["assets"] == ["front.png", "back.png", "preview.jpg"]


def test_the_front_is_served_while_the_back_is_still_being_drawn(
    clean_settings, monkeypatch
) -> None:
    """The render writes the front and its preview a moment before the back. The
    job lists each file as it lands, and the front can be fetched while the
    card is still running."""
    fronted = threading.Event()
    finish = threading.Event()

    def fake_render(stats, *, out_dir, names, **rest):
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / names["front"]).write_bytes(b"front")
        (out_dir / names["preview"]).write_bytes(b"preview")
        fronted.set()
        finish.wait(10)
        (out_dir / names["back"]).write_bytes(b"back")

    monkeypatch.setattr(pipeline_module, "render_card", fake_render)

    from api.main import app

    with TestClient(app) as client:
        card_id = post(client, '{"name": "Sam"}').json()["card_id"]
        try:
            assert fronted.wait(10)
            job = client.get(f"/cards/{card_id}", headers=AUTH).json()
            assert (job["status"], job["assets"]) == ("running", ["front.png", "preview.jpg"])
            front = client.get(f"/cards/{card_id}/front.png", headers=AUTH)
            assert (front.status_code, front.content) == (200, b"front")
            back = client.get(f"/cards/{card_id}/back.png", headers=AUTH)
            assert back.status_code == 404
            assert back.json()["detail"] == "back.png is not ready yet. The card is running."
        finally:
            finish.set()
        job = wait(client, card_id)
        assert (job["status"], job["assets"]) == ("done", ["front.png", "back.png", "preview.jpg"])


def test_the_api_closes_its_browser_when_it_stops(clean_settings, monkeypatch) -> None:
    """Chromium stays up between cards and goes when the api does."""
    closed: list[bool] = []
    monkeypatch.setattr("api.main.CHROMIUM", SimpleNamespace(close=lambda: closed.append(True)))

    from api.main import app

    with TestClient(app):
        assert closed == []
    assert closed == [True]


# -- delete ------------------------------------------------------------------


def test_delete_purges_the_card_the_upload_and_the_row(
    client: TestClient, clean_settings, example_stats: str
) -> None:
    card_id = post(client, example_stats).json()["card_id"]
    wait(client, card_id)

    assert client.delete(f"/cards/{card_id}", headers=AUTH).status_code == 204
    assert client.get(f"/cards/{card_id}", headers=AUTH).status_code == 404
    assert not (clean_settings.data_dir / "uploads" / card_id).exists()
    assert not (clean_settings.data_dir / "cards" / card_id).exists()
    assert not (clean_settings.data_dir / "work" / card_id).exists()


def test_deleting_an_unknown_card_is_a_404(client: TestClient) -> None:
    assert client.delete("/cards/UnknownButWellFormed", headers=AUTH).status_code == 404
