"""The 401 matrix, run against the mock stack through Caddy.

Every route on every service: no header is 401, a wrong key is 401, the right
key is 200. The one exception is /livez. A path the stack does not serve fails
closed: 401 without the key, so a stranger learns nothing about the route list,
and 404 with it.

This runs against the stack the way the internet would reach it, so it covers
Caddy's routing as well as each service's own check.
"""

from __future__ import annotations

import httpx
import pytest

pytestmark = pytest.mark.integration

WRONG_KEY = "w" * 64

CHAT = {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 8}

# Every route the stack serves, the method, the body the right-key call sends,
# and the service behind it.
ROUTES = [
    ("GET", "/healthz", None, "api"),
    ("GET", "/models", None, "api"),
    ("GET", "/v1/models", None, "llm"),
    ("POST", "/v1/chat/completions", CHAT, "llm"),
]

# Paths the stack does not serve. vLLM's open routes are among them: Caddy
# sends only /v1/* to vLLM, so /health and /metrics land on the api's gate.
UNKNOWN = ["/cards", "/cards/abc123", "/health", "/metrics", "/nope", "/v1"]

# One cheap authenticated call per service that keeps a limiter: the api and
# the mock of vLLM. vLLM itself has none.
CLEARING_CALLS = ["/healthz", "/v1/models"]


def call(gateway: str, method: str, path: str, key: str | None, body=None) -> httpx.Response:
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return httpx.request(method, f"{gateway}{path}", headers=headers, json=body, timeout=10)


@pytest.fixture(autouse=True)
def clear_the_failed_auth_counters(gateway: str, live_key: str) -> None:
    """Undo what this file does to the nuisance limit before every test.

    The matrix fails authentication dozens of times from one address, which is
    the pattern common.auth.FailedAuthLimiter answers with 429. A right key
    clears an address's count, so one authenticated call per service puts each
    limiter back to zero. The limit itself is tested on purpose further down.
    """
    for path in CLEARING_CALLS:
        call(gateway, "GET", path, live_key)


def test_livez_is_open(gateway: str) -> None:
    response = httpx.get(f"{gateway}/livez", timeout=10)
    assert (response.status_code, response.text.strip()) == (200, "ok")


@pytest.mark.parametrize(("method", "path", "body", "service"), ROUTES)
def test_no_header_is_401(gateway: str, method: str, path: str, body, service: str) -> None:
    response = call(gateway, method, path, None, body)
    assert response.status_code == 401, f"{method} {path} answered {response.status_code}"


@pytest.mark.parametrize(("method", "path", "body", "service"), ROUTES)
def test_wrong_key_is_401(gateway: str, method: str, path: str, body, service: str) -> None:
    response = call(gateway, method, path, WRONG_KEY, body)
    assert response.status_code == 401, f"{method} {path} answered {response.status_code}"


@pytest.mark.parametrize(("method", "path", "body", "service"), ROUTES)
def test_right_key_is_200(
    gateway: str, live_key: str, method: str, path: str, body, service: str
) -> None:
    response = call(gateway, method, path, live_key, body)
    assert response.status_code == 200, f"{method} {path} answered {response.status_code}"


@pytest.mark.parametrize("path", UNKNOWN)
def test_a_path_the_stack_does_not_serve_fails_closed(
    gateway: str, live_key: str, path: str
) -> None:
    assert call(gateway, "GET", path, None).status_code == 401, path
    assert call(gateway, "GET", path, WRONG_KEY).status_code == 401, path
    assert call(gateway, "GET", path, live_key).status_code == 404, path


@pytest.mark.parametrize("path", ["/livez/../healthz", "/v1/../healthz", "/v1/../../models"])
def test_a_dotted_path_does_not_get_past_the_key(gateway: str, path: str) -> None:
    response = httpx.get(f"{gateway}{path}", timeout=10)
    assert response.status_code in (401, 404), f"{path} answered {response.status_code}"


def test_malformed_auth_headers_are_401(gateway: str, live_key: str) -> None:
    for header in ["", "Bearer", f"Basic {live_key}", live_key, f"Bearer {live_key} extra"]:
        response = httpx.get(f"{gateway}/healthz", headers={"Authorization": header}, timeout=10)
        assert response.status_code == 401, "a malformed header was accepted"


def test_the_api_offers_the_bearer_scheme_on_a_401(gateway: str) -> None:
    for path in ("/healthz", "/models", "/nope"):
        response = httpx.get(f"{gateway}{path}", timeout=10)
        assert response.headers.get("WWW-Authenticate") == "Bearer", path


def test_too_many_wrong_keys_earn_a_429(gateway: str, live_key: str) -> None:
    """The nuisance limit, through Caddy. Not a defence, and SECURITY.md says so.

    It refuses wrong keys only. The right key is served and takes the block with
    it, which is what stops a mistyped key locking someone out of their own node.
    """
    from common.auth import FAIL_LIMIT

    for _ in range(FAIL_LIMIT + 1):
        assert call(gateway, "GET", "/healthz", WRONG_KEY).status_code == 401

    blocked = call(gateway, "GET", "/healthz", WRONG_KEY)
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) > 0

    assert call(gateway, "GET", "/healthz", live_key).status_code == 200
    assert call(gateway, "GET", "/healthz", WRONG_KEY).status_code == 401
    # Leave the counter where the next test expects it.
    call(gateway, "GET", "/healthz", live_key)
