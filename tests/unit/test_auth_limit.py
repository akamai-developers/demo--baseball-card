"""The per-address limit on failed authentications, and the open static paths.

The limiter is a nuisance limit: 20 failures a minute from one address, then a
minute of 429. These tests pin the count, the window, what clears it, and the
fact that it never touches the open paths.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from common.auth import (
    BLOCK_S,
    FAIL_LIMIT,
    FailedAuthLimiter,
    add_auth_gate,
    is_open,
)

KEY = "t" * 64


# -- the limiter on its own --------------------------------------------------


def test_it_takes_more_than_the_limit_to_block() -> None:
    limiter = FailedAuthLimiter()
    for _ in range(FAIL_LIMIT):
        limiter.record_failure("10.0.0.1", now=100.0)
    assert limiter.retry_after("10.0.0.1", now=100.0) == 0

    limiter.record_failure("10.0.0.1", now=100.0)
    assert limiter.retry_after("10.0.0.1", now=100.0) == int(BLOCK_S)


def test_the_block_expires() -> None:
    limiter = FailedAuthLimiter()
    for _ in range(FAIL_LIMIT + 1):
        limiter.record_failure("10.0.0.1", now=100.0)
    assert limiter.retry_after("10.0.0.1", now=100.0 + BLOCK_S - 1) == 1
    assert limiter.retry_after("10.0.0.1", now=100.0 + BLOCK_S) == 0
    # And the history goes with it, so the next failure starts from one.
    limiter.record_failure("10.0.0.1", now=100.0 + BLOCK_S)
    assert limiter.retry_after("10.0.0.1", now=100.0 + BLOCK_S) == 0


def test_failures_older_than_the_window_do_not_count() -> None:
    limiter = FailedAuthLimiter()
    for second in range(FAIL_LIMIT):
        limiter.record_failure("10.0.0.1", now=float(second))
    # Far enough on that every one of those has aged out.
    limiter.record_failure("10.0.0.1", now=200.0)
    assert limiter.retry_after("10.0.0.1", now=200.0) == 0


def test_addresses_are_counted_apart() -> None:
    limiter = FailedAuthLimiter()
    for _ in range(FAIL_LIMIT + 1):
        limiter.record_failure("10.0.0.1", now=100.0)
    assert limiter.retry_after("10.0.0.1", now=100.0) > 0
    assert limiter.retry_after("10.0.0.2", now=100.0) == 0


def test_a_request_that_authenticates_clears_the_count() -> None:
    limiter = FailedAuthLimiter()
    for _ in range(FAIL_LIMIT):
        limiter.record_failure("10.0.0.1", now=100.0)
    limiter.forget("10.0.0.1")
    for _ in range(FAIL_LIMIT):
        limiter.record_failure("10.0.0.1", now=100.0)
    assert limiter.retry_after("10.0.0.1", now=100.0) == 0


# -- the gate ----------------------------------------------------------------


@pytest.fixture
def app(clean_settings) -> FastAPI:
    application = FastAPI()
    add_auth_gate(
        application, open_paths=frozenset({"/livez", "/"}), open_prefixes=("/app/", "/vendor/")
    )

    @application.get("/livez")
    async def livez() -> str:
        return "ok"

    @application.get("/")
    async def index() -> str:
        return "page"

    @application.get("/app/{name}")
    async def asset(name: str) -> str:
        return name

    @application.get("/cards")
    async def cards() -> list[str]:
        return []

    return application


def test_the_open_paths_need_no_key(app: FastAPI) -> None:
    with TestClient(app) as client:
        assert client.get("/livez").status_code == 200
        assert client.get("/").status_code == 200
        assert client.get("/app/app.css").status_code == 200


def test_everything_else_still_needs_one(app: FastAPI) -> None:
    with TestClient(app) as client:
        assert client.get("/cards").status_code == 401
        assert client.get("/cards", headers={"Authorization": f"Bearer {KEY}"}).status_code == 200


def test_a_dotted_path_is_never_open(app: FastAPI) -> None:
    """/app/../cards must not be handed on as an open request."""
    assert is_open("/app/../cards", frozenset({"/"}), ("/app/",)) is False
    with TestClient(app) as client:
        assert client.get("/app/../cards").status_code == 401


def test_going_past_the_limit_earns_a_429(app: FastAPI) -> None:
    """The 21st failure in a minute is still answered 401, and starts the block.
    Everything after it gets 429 until the minute is up."""
    with TestClient(app) as client:
        for _ in range(FAIL_LIMIT + 1):
            assert client.get("/cards").status_code == 401

        blocked = client.get("/cards")
        assert blocked.status_code == 429
        assert 0 < int(blocked.headers["Retry-After"]) <= BLOCK_S


def test_the_right_key_is_served_and_clears_the_block(app: FastAPI) -> None:
    """The limit refuses wrong keys, not right ones. Someone who pastes a bad key
    twenty times and then the real one gets their card, not a minute of 429."""
    with TestClient(app) as client:
        for _ in range(FAIL_LIMIT + 1):
            client.get("/cards")
        assert client.get("/cards").status_code == 429

        assert client.get("/cards", headers={"Authorization": f"Bearer {KEY}"}).status_code == 200
        # And the block went with it: the next wrong key is a plain 401 again.
        assert client.get("/cards").status_code == 401


def test_a_loop_that_keeps_guessing_stays_blocked(app: FastAPI) -> None:
    with TestClient(app) as client:
        for _ in range(FAIL_LIMIT + 1):
            client.get("/cards")
        for _ in range(10):
            assert client.get("/cards").status_code == 429


def test_the_open_paths_keep_working_while_an_address_is_blocked(app: FastAPI) -> None:
    """The page itself must still load, or a mistyped key locks someone out of
    the field they would fix it in."""
    with TestClient(app) as client:
        for _ in range(FAIL_LIMIT + 1):
            client.get("/cards")
        assert client.get("/").status_code == 200
        assert client.get("/livez").status_code == 200


def test_the_forwarded_address_is_the_one_counted(app: FastAPI) -> None:
    """Behind Caddy every request has the same peer address, so the limiter reads
    the last entry of X-Forwarded-For. Caddy appends the address it saw, so a
    forged header cannot move the count onto someone else."""
    with TestClient(app) as client:
        for _ in range(FAIL_LIMIT + 1):
            client.get("/cards", headers={"X-Forwarded-For": "203.0.113.9"})
        assert client.get("/cards", headers={"X-Forwarded-For": "203.0.113.9"}).status_code == 429
        # A different address, and the forged entry in front of it, both pass.
        assert client.get("/cards", headers={"X-Forwarded-For": "198.51.100.4"}).status_code == 401
        assert (
            client.get("/cards", headers={"X-Forwarded-For": "203.0.113.9, 198.51.100.4"})
        ).status_code == 401
