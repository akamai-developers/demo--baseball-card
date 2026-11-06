"""Five cards at once, in one api process.

Two things could go wrong when jobs overlap and neither shows up with one job at
a time. Chromium is a few hundred MB per render, so the renders have to queue at
RENDER_CONCURRENCY instead of all starting at once. And every step row carries a
card_id, so a job writing its ledger while four others write theirs has to write
only its own rows.

The gateway version of this is tests/integration/test_concurrency.py. It runs the
real Chromium. This one stubs the render out, which is what lets it hold a real
lock and count what overlapped.
"""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from api import pipeline as pipeline_module
from api.pipeline import STEPS
from tests.conftest import TEST_KEY

AUTH = {"Authorization": f"Bearer {TEST_KEY}"}
JOBS = 5
NAMES = ["Sam Rivera", "Ada Okafor", "Milo Strand", "Jun Park", "Rosa Delgado"]


class RenderCounter:
    """How many fake renders were inside at once, and the most there ever were.

    render_card runs in a worker thread, so this is a threading lock and not an
    asyncio one.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.inside = 0
        self.peak = 0
        self.calls = 0

    def enter(self) -> None:
        with self.lock:
            self.inside += 1
            self.calls += 1
            self.peak = max(self.peak, self.inside)

    def leave(self) -> None:
        with self.lock:
            self.inside -= 1


@pytest.fixture
def counter() -> RenderCounter:
    return RenderCounter()


@pytest.fixture
def client(clean_settings, monkeypatch, counter: RenderCounter) -> TestClient:
    """The api with a render that takes long enough for five jobs to collide."""

    def fake_render(stats, *, style, art, cutout, out_dir, text, comps, steps, names):
        counter.enter()
        try:
            # Long enough that jobs started together are still here together, and
            # short enough that five of them through two slots is a second.
            time.sleep(0.3)
            out_dir.mkdir(parents=True, exist_ok=True)
            written = {}
            for side in pipeline_module.ASSET_NAMES:
                path = out_dir / names[side]
                # The name goes in the file, so a crossed render would show up as
                # a card holding another card's bytes.
                path.write_text(stats.name)
                written[side] = path
            return written
        finally:
            counter.leave()

    monkeypatch.setattr(pipeline_module, "render_card", fake_render)

    from api.main import app

    with TestClient(app) as test_client:
        yield test_client


def photo(colour: tuple[int, int, int]) -> bytes:
    import io

    buffer = io.BytesIO()
    Image.new("RGB", (400, 300), colour).save(buffer, "JPEG")
    return buffer.getvalue()


def start_all(client: TestClient) -> dict[str, str]:
    """Post every job before waiting on any of them. Returns card_id -> name."""
    started = {}
    for i, name in enumerate(NAMES):
        response = client.post(
            "/cards",
            headers=AUTH,
            files={"photo": ("me.jpg", photo((200, 200 - i * 10, 200)), "image/jpeg")},
            data={"stats": f'{{"name": "{name}", "number": {i + 1}}}'},
        )
        assert response.status_code == 202, response.text
        started[response.json()["card_id"]] = name
    return started


def wait_all(client: TestClient, card_ids, timeout: float = 60.0) -> dict[str, dict]:
    deadline = time.monotonic() + timeout
    jobs: dict[str, dict] = {}
    while time.monotonic() < deadline:
        jobs = {
            card_id: client.get(f"/cards/{card_id}", headers=AUTH).json() for card_id in card_ids
        }
        if all(job["status"] in ("done", "failed") for job in jobs.values()):
            return jobs
        time.sleep(0.05)
    still = {card_id: job["status"] for card_id, job in jobs.items()}
    raise AssertionError(f"not every job finished in {timeout:.0f}s: {still}")


@pytest.fixture
def five(client: TestClient) -> dict:
    started = start_all(client)
    return {"client": client, "started": started, "jobs": wait_all(client, started)}


def test_all_five_finish(five: dict) -> None:
    for card_id, job in five["jobs"].items():
        assert job["status"] == "done", f"{card_id}: {job.get('error')}"


def test_five_jobs_get_five_card_ids(five: dict) -> None:
    assert len(set(five["started"])) == JOBS


def test_the_renders_queued_at_the_slot_count(five: dict, counter: RenderCounter, request) -> None:
    """Two slots, five jobs. Never three at once, and never only one at a time."""
    settings = request.getfixturevalue("clean_settings")
    assert counter.calls == JOBS
    assert counter.peak <= settings.render_concurrency, (
        f"{counter.peak} renders ran at once with RENDER_CONCURRENCY={settings.render_concurrency}"
    )
    assert counter.peak >= 2, "no two renders overlapped, so this proved nothing"


def test_every_step_row_belongs_to_its_own_card(five: dict) -> None:
    """The ledger, read straight out of SQLite. Thirty rows, six per card, no strays."""
    db = five["client"].app.state.db
    with db.connect() as connection:
        rows = connection.execute("SELECT card_id, step FROM steps").fetchall()

    assert len(rows) == JOBS * len(STEPS)
    per_card: dict[str, list[str]] = {}
    for card_id, step in rows:
        per_card.setdefault(card_id, []).append(step)

    assert set(per_card) == set(five["started"])
    for card_id, steps in per_card.items():
        assert sorted(steps) == sorted(STEPS), f"{card_id} has {steps}"


def test_each_job_reports_only_its_own_steps(five: dict) -> None:
    for card_id, job in five["jobs"].items():
        names = [step["step"] for step in job["steps"]]
        assert names == list(STEPS), f"{card_id} reported {names}"


def test_every_card_holds_its_own_render(five: dict) -> None:
    """The name was written into the PNG bytes, so a crossed render is readable."""
    storage = five["client"].app.state.storage
    for card_id, name in five["started"].items():
        for asset in ("front.png", "back.png", "preview.jpg"):
            path = storage.asset_path(card_id, asset)
            assert path.exists(), f"{card_id} has no {asset}"
            assert path.read_text() == name, f"{card_id} {asset} came out of another job"
