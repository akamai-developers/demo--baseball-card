"""The art service's pool, through Caddy, on the mock stack.

A card's background is taken from backgrounds the art service painted before
the card asked, and the ledger says so. /healthz says how full the pool is and
which card each service stands on. Skips itself when the stack is not up.
"""

from __future__ import annotations

import time

import httpx
import pytest

from card.backgrounds import recipe
from common.meta import ART_SOURCE, RAN_ON
from common.schemas import STYLES
from tests.integration.test_cards import auth, create, finish, remove

pytestmark = pytest.mark.integration

# A full pool on a stack that just started: two styles, three each, a fraction
# of a second a background for the mock.
FILL_S = 60.0


def health(gateway: str, key: str) -> dict:
    return httpx.get(f"{gateway}/healthz", headers=auth(key), timeout=15).json()


def full(gateway: str, key: str) -> dict:
    """The health report, once the art service's pool is full."""
    deadline = time.monotonic() + FILL_S
    while True:
        body = health(gateway, key)
        art = body["services"]["art"]
        if art["pool_size"] and art["pool_ready"] == art["pool_size"]:
            return body
        assert time.monotonic() < deadline, f"the pool is {art['pool_ready']}/{art['pool_size']}"
        time.sleep(0.5)


def paint(gateway: str, key: str, style: str, **changes) -> httpx.Response:
    body = {**recipe(style), "seed": 4242, **changes}
    return httpx.post(f"{gateway}/art/generate", headers=auth(key), json=body, timeout=60)


def test_healthz_says_how_full_the_pool_is_and_where_each_service_runs(
    gateway: str, live_key: str, mock_backends
) -> None:
    body = full(gateway, live_key)
    art = body["services"]["art"]
    assert art["pool_size"] > 0
    assert art["pool_size"] % len(STYLES) == 0, "the pool is per style"
    # The one-GPU node's layout, mock- in front: every model on card 0.
    assert {name: service["ran_on"] for name, service in body["services"].items()} == {
        "cutout": "mock-gpu0",
        "art": "mock-gpu0",
        "llm": "mock-gpu0",
    }
    assert body["services"]["cutout"]["pool_size"] is None


def test_a_card_takes_its_background_from_the_pool(
    gateway: str, live_key: str, portrait, example_stats: str, mock_backends
) -> None:
    full(gateway, live_key)
    response = create(gateway, live_key, portrait.read_bytes(), example_stats)
    assert response.status_code == 202, response.text
    card_id = response.json()["card_id"]
    try:
        job = finish(gateway, live_key, card_id)
        assert job["status"] == "done", job
        art = next(step for step in job["steps"] if step["step"] == "art")
        assert (art["status"], art["source"], art["ran_on"]) == ("done", "pool", "mock-gpu0")
        assert art["error"] is None
        # A file read and a PNG over the network, not a painting.
        assert art["ms"] < 2000
        # Only the art step has a source.
        sources = {step["step"]: step["source"] for step in job["steps"] if step["source"]}
        assert sources == {"art": "pool"}
    finally:
        remove(gateway, live_key, card_id)
    # The painter puts one back.
    full(gateway, live_key)


def test_three_cards_in_a_style_get_three_backgrounds(
    gateway: str, live_key: str, mock_backends
) -> None:
    """Each takes its own. None is served twice."""
    full(gateway, live_key)
    answers = [paint(gateway, live_key, "classic") for _ in range(3)]

    assert [answer.status_code for answer in answers] == [200, 200, 200]
    assert {answer.headers[ART_SOURCE] for answer in answers} == {"pool"}
    assert len({answer.content for answer in answers}) == 3
    assert {answer.headers[RAN_ON] for answer in answers} == {"mock-gpu0"}
    full(gateway, live_key)


def test_fresh_is_painted_with_the_seed_asked_for(gateway: str, live_key: str) -> None:
    first = paint(gateway, live_key, "akamai", fresh=True)
    again = paint(gateway, live_key, "akamai", fresh=True)

    assert first.headers[ART_SOURCE] == again.headers[ART_SOURCE] == "fresh"
    assert first.content == again.content


def test_a_prompt_of_your_own_is_never_a_pooled_stadium(gateway: str, live_key: str) -> None:
    answer = paint(gateway, live_key, "akamai", prompt="a lighthouse at dusk, no text")
    assert answer.status_code == 200
    assert answer.headers[ART_SOURCE] == "fresh"
