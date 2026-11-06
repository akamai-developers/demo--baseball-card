"""Retention. A photo of someone's face is not interesting after the demo."""

from __future__ import annotations

import asyncio
import io
import time

import pytest
from PIL import Image

from api import sweeper
from api.db import Database
from api.storage import Storage
from common.schemas import Stats
from tests.conftest import TEST_KEY

STATS = Stats(name="Sam Rivera")
AUTH = {"Authorization": f"Bearer {TEST_KEY}"}


@pytest.fixture
def parts(tmp_path):
    storage = Storage(tmp_path / "data")
    storage.prepare()
    db = Database(storage.db_path)
    db.init()
    return db, storage


def age(db: Database, card_id: str, hours: float) -> None:
    with db.connect() as connection:
        connection.execute(
            "UPDATE cards SET created_at = ? WHERE card_id = ?",
            (time.time() - hours * 3600, card_id),
        )


def add(db: Database, storage: Storage, card_id: str, age_hours: float) -> None:
    db.create_card(card_id, STATS, "akamai", ("cutout",))
    storage.write_photo(card_id, b"photo")
    storage.card_dir(card_id).mkdir(parents=True, exist_ok=True)
    (storage.card_dir(card_id) / "front.png").write_bytes(b"front")
    age(db, card_id, age_hours)


def test_a_card_inside_the_window_is_left_alone(parts) -> None:
    db, storage = parts
    add(db, storage, "fresh", age_hours=1)
    assert sweeper.sweep(db, storage, retention_hours=24) == 0
    assert db.get_job("fresh") is not None


def test_a_card_past_the_window_is_removed_with_its_files(parts) -> None:
    db, storage = parts
    add(db, storage, "old", age_hours=48)
    assert sweeper.sweep(db, storage, retention_hours=24) == 1
    assert db.get_job("old") is None
    assert not storage.upload_dir("old").exists()
    assert not storage.card_dir("old").exists()


def test_the_sweep_only_takes_what_it_should(parts) -> None:
    db, storage = parts
    add(db, storage, "fresh", age_hours=1)
    add(db, storage, "old", age_hours=48)
    assert sweeper.sweep(db, storage, retention_hours=24) == 1
    assert db.count() == 1
    assert db.get_job("fresh") is not None


def test_an_empty_database_sweeps_nothing(parts) -> None:
    db, storage = parts
    assert sweeper.sweep(db, storage, retention_hours=24) == 0


async def test_the_loop_sweeps_as_soon_as_it_starts(parts) -> None:
    """The api starts the loop with the node. A card left from before a
    restart that is past the window goes on the first pass, not an interval
    later."""
    db, storage = parts
    add(db, storage, "old", age_hours=48)
    loop = asyncio.create_task(sweeper.run(db, storage, retention_hours=24, interval_s=900))
    try:
        for _ in range(100):
            if db.get_job("old") is None:
                break
            await asyncio.sleep(0.02)
        assert db.get_job("old") is None
        assert not storage.upload_dir("old").exists()
    finally:
        loop.cancel()
        with pytest.raises(asyncio.CancelledError):
            await loop


async def test_an_error_does_not_end_the_loop(parts, monkeypatch) -> None:
    db, storage = parts
    calls: list[int] = []

    def broken(*args) -> int:
        calls.append(1)
        raise RuntimeError("the disk is gone")

    monkeypatch.setattr(sweeper, "sweep", broken)
    loop = asyncio.create_task(sweeper.run(db, storage, retention_hours=24, interval_s=900))
    await asyncio.sleep(0.1)
    assert calls == [1]
    assert not loop.done()
    loop.cancel()


# -- end to end --------------------------------------------------------------
#
# The tests above call sweep() on a database a test filled by hand. These make a
# real card through the api first, so what gets deleted is a real photo, a real
# row, and the three real files, and the api's own answer afterwards is checked
# too.


@pytest.fixture
def client(clean_settings, monkeypatch):
    """The api with Chromium stubbed out."""
    from api import pipeline as pipeline_module

    def fake_render(stats, *, out_dir, names, **rest):
        out_dir.mkdir(parents=True, exist_ok=True)
        for side in pipeline_module.ASSET_NAMES:
            (out_dir / names[side]).write_bytes(b"rendered")

    monkeypatch.setattr(pipeline_module, "render_card", fake_render)

    from fastapi.testclient import TestClient

    from api.main import app

    with TestClient(app) as test_client:
        yield test_client


def make_a_card(client) -> str:
    """Post the smallest card the api will take, and wait for it."""
    buffer = io.BytesIO()
    Image.new("RGB", (400, 300), (200, 200, 200)).save(buffer, "JPEG")
    created = client.post(
        "/cards",
        headers=AUTH,
        files={"photo": ("me.jpg", buffer.getvalue(), "image/jpeg")},
        data={"stats": '{"name": "Sam Rivera"}'},
    )
    assert created.status_code == 202, created.text
    card_id = created.json()["card_id"]
    for _ in range(200):
        job = client.get(f"/cards/{card_id}", headers=AUTH).json()
        if job["status"] in ("done", "failed"):
            assert job["status"] == "done", job.get("error")
            return card_id
        time.sleep(0.05)
    raise AssertionError(f"card {card_id} never finished")


def test_a_card_past_the_window_is_gone_from_the_api_after_a_sweep(client) -> None:
    card_id = make_a_card(client)
    storage = client.app.state.storage
    db = client.app.state.db
    assert storage.photo_path(card_id).exists()
    assert client.get(f"/cards/{card_id}/front.png", headers=AUTH).status_code == 200

    age(db, card_id, 25)
    assert sweeper.sweep(db, storage, retention_hours=24) == 1

    assert not storage.upload_dir(card_id).exists()
    assert not storage.card_dir(card_id).exists()
    assert not storage.work_dir(card_id).exists()
    assert client.get(f"/cards/{card_id}", headers=AUTH).status_code == 404
    assert client.get(f"/cards/{card_id}/front.png", headers=AUTH).status_code == 404


def test_delete_purges_the_same_things_the_sweeper_would(client) -> None:
    """DELETE is the sweeper for one card, on demand. It has to leave the same nothing."""
    card_id = make_a_card(client)
    storage = client.app.state.storage

    assert client.delete(f"/cards/{card_id}", headers=AUTH).status_code == 204

    assert client.app.state.db.get_job(card_id) is None
    assert not storage.upload_dir(card_id).exists()
    assert not storage.card_dir(card_id).exists()
    assert not storage.work_dir(card_id).exists()
    assert client.get(f"/cards/{card_id}", headers=AUTH).status_code == 404
    age(client.app.state.db, card_id, 25)
    # And the sweeper then has nothing left to take.
    assert sweeper.sweep(client.app.state.db, storage, retention_hours=24) == 0
