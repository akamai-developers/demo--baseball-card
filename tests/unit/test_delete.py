"""Deleting a card while it is still being made.

DELETE /cards/{id} removes the card's rows, then stops its task, and waits for
it before it removes the files. Nothing the pipeline writes after the rows are
gone lands anywhere. A thread the job started cannot be stopped, so the task
ends after it, and no file the job was writing turns up after the purge.
"""

from __future__ import annotations

import asyncio
import io
import json
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from api import pipeline as pipeline_module
from api.db import Database
from api.pipeline import ASSET_NAMES, STEPS, Pipeline
from api.storage import Storage
from common.meta import model_headers
from common.schemas import Stats
from tests.conftest import TEST_KEY

AUTH = {"Authorization": f"Bearer {TEST_KEY}"}
STATS = Stats(name="Sam Rivera", metrics={"exit_velo_mph": 88.5})
CARD_ID = "delete-card-0001"


def png(size: tuple[int, int], mode: str = "RGB") -> bytes:
    buffer = io.BytesIO()
    Image.new(mode, size, (30, 60, 90) if mode == "RGB" else (30, 60, 90, 255)).save(buffer, "PNG")
    return buffer.getvalue()


# -- the pipeline ------------------------------------------------------------


@pytest.fixture
def parts(clean_settings, monkeypatch):
    """A pipeline with every service configured, on a throwaway volume."""
    monkeypatch.setenv("CUTOUT_URL", "http://cutout:8001")
    monkeypatch.setenv("ART_URL", "http://art:8002")
    monkeypatch.setenv("LLM_URL", "http://llm:8000")
    monkeypatch.setenv("LLM_MODEL", "a-model")
    from common.settings import get_settings

    get_settings.cache_clear()
    settings = get_settings()
    storage = Storage(settings.data_dir)
    storage.prepare()
    database = Database(storage.db_path)
    database.init()
    storage.write_photo(CARD_ID, png((100, 140)))
    database.create_card(CARD_ID, STATS, "akamai", STEPS)
    pipeline = Pipeline(CARD_ID, STATS, "akamai", settings, database, storage)
    return SimpleNamespace(settings=settings, db=database, storage=storage, pipeline=pipeline)


# Passes api.text.check for STATS, so the text step ends done.
TEXT = json.dumps(
    {
        "nickname": "Turnstile",
        "tagline": "Quick hands, short path",
        "blurb": "The ball leaves the bat at 88.5 mph. Gap to gap.",
    }
)


def answering(hold: dict[str, asyncio.Event]) -> httpx.MockTransport:
    """Every service answers at once, except the paths in `hold`, which wait for their event."""

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path in hold:
            await hold[path].wait()
        if path == "/cutout":
            headers = model_headers("ZhengPeng7/BiRefNet", "MIT", 300, "akamai-gpu0")
            return httpx.Response(200, content=png((80, 100), "RGBA"), headers=headers)
        if path == "/art/generate":
            headers = model_headers("black-forest-labs/FLUX.2-klein-4B", "Apache-2.0", 900, "g0")
            return httpx.Response(200, content=png((100, 140)), headers=headers)
        if path == "/v1/chat/completions":
            body = {"model": "a-model", "choices": [{"message": {"content": TEXT}}]}
            return httpx.Response(200, json=body)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def use(monkeypatch, transport: httpx.MockTransport) -> None:
    real = httpx.AsyncClient

    def build(**kwargs):
        kwargs.pop("transport", None)
        return real(transport=transport, **kwargs)

    monkeypatch.setattr(pipeline_module.httpx, "AsyncClient", build)


def rendering(monkeypatch, held: threading.Event | None = None) -> SimpleNamespace:
    """Stand in for Chromium. With `held`, the render waits in its thread for it."""
    seen = SimpleNamespace(calls=0, started=threading.Event(), ended=threading.Event())

    def fake(stats, *, out_dir, names, **rest):
        seen.calls += 1
        seen.started.set()
        if held is not None:
            held.wait(10)
        out_dir.mkdir(parents=True, exist_ok=True)
        for side in ASSET_NAMES:
            (out_dir / names[side]).write_bytes(b"rendered")
        seen.ended.set()

    monkeypatch.setattr(pipeline_module, "render_card", fake)
    return seen


async def until(check, seconds: float = 5.0) -> None:
    deadline = time.monotonic() + seconds
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError("it never happened")
        await asyncio.sleep(0.01)


def status_of(db: Database, step: str) -> str:
    return next(row.status for row in db.get_job(CARD_ID).steps if row.step == step)


async def test_a_delete_stops_the_job_where_it_is(parts, monkeypatch) -> None:
    """The cutout is waiting on its service. The card stops there, makes
    nothing, and leaves no row behind."""
    cutout = asyncio.Event()
    use(monkeypatch, answering({"/cutout": cutout}))
    render = rendering(monkeypatch)
    task = asyncio.create_task(parts.pipeline.run())
    await until(lambda: status_of(parts.db, "cutout") == "running")
    await until(lambda: status_of(parts.db, "text") == "done")

    assert parts.db.delete_card(CARD_ID)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    cutout.set()
    await asyncio.sleep(0.05)

    assert parts.db.get_job(CARD_ID) is None
    assert parts.db.count() == 0
    assert render.calls == 0
    assert not parts.storage.card_dir(CARD_ID).exists()


async def test_a_thread_in_flight_ends_before_the_task_does(parts, monkeypatch) -> None:
    """Chromium is rendering when the delete lands. The task waits for it, and
    nothing it wrote is left under the card's names."""
    use(monkeypatch, answering({}))
    held = threading.Event()
    render = rendering(monkeypatch, held)
    task = asyncio.create_task(parts.pipeline.run())
    assert await asyncio.to_thread(render.started.wait, 5)

    assert parts.db.delete_card(CARD_ID)
    task.cancel()
    await asyncio.sleep(0.2)
    assert not task.done(), "the task ended while its render thread was still writing"
    held.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert render.ended.is_set()
    assert parts.db.get_job(CARD_ID) is None
    for name in ASSET_NAMES.values():
        assert not parts.storage.asset_path(CARD_ID, name).exists()


async def test_a_card_nobody_deletes_is_made_as_before(parts, monkeypatch) -> None:
    use(monkeypatch, answering({}))
    rendering(monkeypatch)
    await parts.pipeline.run()
    assert parts.db.get_job(CARD_ID).status == "done"
    assert parts.storage.asset_path(CARD_ID, "front.png").is_file()


# -- the route ---------------------------------------------------------------


@pytest.fixture
def held(monkeypatch):
    """The render, held in its thread until the test lets it go."""
    gate = threading.Event()
    seen = rendering(monkeypatch, gate)
    seen.release = gate
    yield seen
    gate.set()  # never leave a thread waiting


@pytest.fixture
def client(clean_settings, held) -> TestClient:
    """The api with no model service configured, so every card goes straight to
    the render, and the render held."""
    from api.main import app

    with TestClient(app) as test_client:
        yield test_client
        # A held render would hold the api's drain on the way out.
        held.release.set()


def post(client: TestClient) -> str:
    response = client.post(
        "/cards",
        headers=AUTH,
        files={"photo": ("me.jpg", png((400, 300)), "image/png")},
        data={"stats": '{"name": "Sam Rivera"}'},
    )
    assert response.status_code == 202
    return response.json()["card_id"]


def settle(client: TestClient, seconds: float = 5.0) -> None:
    """Wait until the api holds no card's task."""
    deadline = time.monotonic() + seconds
    while client.app.state.jobs.running:
        if time.monotonic() > deadline:
            raise AssertionError("a card's task is still running")
        time.sleep(0.02)


def test_a_delete_in_the_middle_of_a_render_leaves_nothing_behind(
    client: TestClient, held, clean_settings
) -> None:
    """The delete stops the card and waits for Chromium to finish before it
    purges, so the files Chromium was writing go with the rest."""
    card_id = post(client)
    assert held.started.wait(10)
    threading.Timer(0.5, held.release.set).start()

    began = time.monotonic()
    assert client.delete(f"/cards/{card_id}", headers=AUTH).status_code == 204
    assert time.monotonic() - began >= 0.4, "the delete did not wait for the render"
    assert held.ended.is_set()

    settle(client)
    data = clean_settings.data_dir
    for folder in ("uploads", "cards", "work"):
        assert not (data / folder / card_id).exists(), f"{folder}/{card_id} is still there"
    assert client.get(f"/cards/{card_id}", headers=AUTH).status_code == 404


def test_a_deleted_card_is_gone_at_once_while_its_render_finishes(client: TestClient, held) -> None:
    """The rows go before the wait, so the app polling the card sees a 404
    straight away, not a card that says running."""
    card_id = post(client)
    assert held.started.wait(10)
    gone: list[int] = []

    def poll() -> None:
        time.sleep(0.2)
        gone.append(client.get(f"/cards/{card_id}", headers=AUTH).status_code)
        held.release.set()

    threading.Thread(target=poll).start()
    assert client.delete(f"/cards/{card_id}", headers=AUTH).status_code == 204
    assert gone == [404]


def test_deleting_needs_the_key(client: TestClient, held) -> None:
    card_id = post(client)
    assert client.delete(f"/cards/{card_id}").status_code == 401
    wrong = {"Authorization": "Bearer " + "w" * 64}
    assert client.delete(f"/cards/{card_id}", headers=wrong).status_code == 401
    held.release.set()
    settle(client)
    assert client.get(f"/cards/{card_id}", headers=AUTH).json()["status"] == "done"


def test_deleting_a_card_id_that_could_not_exist_is_a_404(client: TestClient) -> None:
    assert client.delete("/cards/UnknownButWellFormed", headers=AUTH).status_code == 404
    assert client.delete("/cards/..%2F..%2Fetc", headers=AUTH).status_code == 404
