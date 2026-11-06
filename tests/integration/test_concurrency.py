"""Five cards at once, through Caddy, with the real Chromium behind it.

tests/unit/test_concurrency.py stubs the render out so it can count what
overlapped and read the step rows out of SQLite. This one runs the stack a user
runs: five uploads posted at the same moment, five real browsers queued at
RENDER_CONCURRENCY, five real PNGs at the end.

Skips itself when the stack is not up. Start it with: make up-mock
"""

from __future__ import annotations

import io
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from PIL import Image

from api.pipeline import STEPS
from card import CARD_H, CARD_W

pytestmark = pytest.mark.integration

JOBS = 5
TIMEOUT = 300.0
NAMES = ["Sam Rivera", "Ada Okafor", "Milo Strand", "Jun Park", "Rosa Delgado"]


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def stats_for(name: str, number: int) -> str:
    return (
        f'{{"name": "{name}", "number": {number}, "position": "SS",'
        f' "metrics": {{"bat_speed_mph": {65 + number}, "exit_velo_mph": {85 + number}}}}}'
    )


@pytest.fixture(scope="module")
def five(request) -> dict:
    """Five cards, posted at once, waited on together, deleted at the end."""
    gateway = request.getfixturevalue("gateway")
    key = request.getfixturevalue("live_key")
    request.getfixturevalue("mock_backends")
    photo = request.getfixturevalue("portrait").read_bytes()

    def post(item: tuple[int, str]) -> tuple[str, str]:
        number, name = item
        response = httpx.post(
            f"{gateway}/cards",
            headers=auth(key),
            files={"photo": ("me.jpg", photo, "image/jpeg")},
            data={"stats": stats_for(name, number)},
            timeout=60,
        )
        assert response.status_code == 202, response.text
        return response.json()["card_id"], name

    began = time.monotonic()
    with ThreadPoolExecutor(max_workers=JOBS) as pool:
        started = dict(pool.map(post, enumerate(NAMES, start=1)))
    posted_in = time.monotonic() - began

    deadline = time.monotonic() + TIMEOUT
    jobs: dict[str, dict] = {}
    while time.monotonic() < deadline:
        jobs = {
            card_id: httpx.get(f"{gateway}/cards/{card_id}", headers=auth(key), timeout=30).json()
            for card_id in started
        }
        if all(job["status"] in ("done", "failed") for job in jobs.values()):
            break
        time.sleep(0.5)

    made = {
        "gateway": gateway,
        "key": key,
        "started": started,
        "jobs": jobs,
        "posted_in": posted_in,
        "took": time.monotonic() - began,
    }
    yield made
    for card_id in started:
        httpx.delete(f"{gateway}/cards/{card_id}", headers=auth(key), timeout=30)


def test_five_jobs_were_really_in_flight_together(five: dict) -> None:
    """All five uploads landed before the first card could have finished."""
    assert len(set(five["started"])) == JOBS
    assert five["posted_in"] < 10.0, f"posting took {five['posted_in']:.1f}s, so they queued"


def test_all_five_finish(five: dict) -> None:
    unfinished = {
        card_id: job["status"] for card_id, job in five["jobs"].items() if job["status"] != "done"
    }
    assert not unfinished, f"{unfinished} (after {five['took']:.0f}s)"


def test_each_card_has_its_own_six_step_ledger(five: dict) -> None:
    for card_id, job in five["jobs"].items():
        names = [step["step"] for step in job["steps"]]
        assert names == list(STEPS), f"{card_id} reported {names}"
        assert all(step["status"] in ("done", "skipped") for step in job["steps"]), job["steps"]


def test_every_card_came_out_at_card_size(five: dict) -> None:
    for card_id in five["started"]:
        for asset in ("front.png", "back.png"):
            response = httpx.get(
                f"{five['gateway']}/cards/{card_id}/{asset}",
                headers=auth(five["key"]),
                timeout=60,
            )
            assert response.status_code == 200, f"{card_id}/{asset}: {response.status_code}"
            assert Image.open(io.BytesIO(response.content)).size == (CARD_W, CARD_H)


def test_the_five_cards_are_five_different_pictures(five: dict) -> None:
    """Different names and numbers, so the fronts cannot be byte-identical."""
    fronts = set()
    for card_id in five["started"]:
        response = httpx.get(
            f"{five['gateway']}/cards/{card_id}/front.png",
            headers=auth(five["key"]),
            timeout=60,
        )
        fronts.add(response.content)
    assert len(fronts) == JOBS
