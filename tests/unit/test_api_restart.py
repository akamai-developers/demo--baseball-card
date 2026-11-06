"""A card cut off when the api stops is marked failed when it starts again.

The api is one process and holds each card's pipeline in memory, so a stop in
the middle of a card leaves its row saying running, and the app polls it for
good. Two fixes, both held here. Compose gives a stopping api longer than it
takes to drain the cards it holds, and at start the api marks whatever is
still unfinished as failed, with the reason. tests/unit/test_db.py holds the
query itself.
"""

from __future__ import annotations

import inspect

import pytest
import yaml
from fastapi.testclient import TestClient

from api.db import Database
from api.pipeline import Jobs
from api.storage import Storage
from common.schemas import Stats
from tests.conftest import REPO_ROOT, TEST_KEY

AUTH = {"Authorization": f"Bearer {TEST_KEY}"}
REASON = "the api restarted before this card was finished"


@pytest.fixture
def db(tmp_path) -> Database:
    database = Database(tmp_path / "cards.db")
    database.init()
    return database


@pytest.fixture
def stats() -> Stats:
    return Stats(name="Sam Rivera", metrics={"bat_speed_mph": 68.2})


def test_the_api_marks_them_when_it_starts(clean_settings) -> None:
    """Through the api's own start, and read back through its route."""
    storage = Storage(clean_settings.data_dir)
    storage.prepare()
    db = Database(storage.db_path)
    db.init()
    db.create_card("cut_off_by_a_restart", Stats(name="Sam Rivera"), "akamai", ["cutout", "art"])
    db.set_status("cut_off_by_a_restart", "running")
    db.update_step("cut_off_by_a_restart", "cutout", status="running")

    from api.main import CUT_OFF, app

    assert CUT_OFF == REASON
    with TestClient(app) as client:
        job = client.get("/cards/cut_off_by_a_restart", headers=AUTH).json()
    assert job["status"] == "failed"
    assert job["error"] == REASON
    assert [step["status"] for step in job["steps"]] == ["failed", "skipped"]


def test_a_stopping_api_is_given_longer_than_its_drain() -> None:
    """Docker's default is 10 s, then SIGKILL, and the drain waits up to 30."""
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    grace = compose["services"]["api"]["stop_grace_period"]
    assert grace.endswith("s"), grace
    drain = inspect.signature(Jobs.drain).parameters["timeout"].default
    assert int(grace[:-1]) > drain
