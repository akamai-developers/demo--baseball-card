"""The 401 matrix, run against the mock stack through Caddy.

Every route on every service: no header is 401, a wrong key is 401, the right
key is the route's own answer, 200, 202 for a new card and 204 for a delete.
The exceptions are /livez and the page: `/`, `/app/*` and `/vendor/*`. A path
the stack does not serve fails closed: 401 without the key, so a stranger
learns nothing about the route list, and 404 with it.

This runs against the stack the way the internet would reach it, so it covers
Caddy's routing as well as each service's own check.
"""

from __future__ import annotations

import time

import httpx
import pytest

pytestmark = pytest.mark.integration

WRONG_KEY = "w" * 64

CHAT = {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 8}
ART = {"prompt": "a stadium", "style": "akamai", "width": 256, "height": 352}


def photo(portrait) -> dict:
    return {"photo": ("me.jpg", portrait.read_bytes(), "image/jpeg")}


# Every route the stack serves: the method, the path, what the call sends, the
# answer the right key gets, and the service behind it. {card} is a card made
# for this file. {doomed} is a card made for the one call that deletes it.
ROUTES = [
    ("GET", "/healthz", None, 200, "api"),
    ("GET", "/models", None, 200, "api"),
    ("POST", "/cards", "card", 202, "api"),
    ("GET", "/cards/{card}", None, 200, "api"),
    ("GET", "/cards/{card}/front.png", None, 200, "api"),
    ("GET", "/cards/{card}/back.png", None, 200, "api"),
    ("GET", "/cards/{card}/preview.jpg", None, 200, "api"),
    ("DELETE", "/cards/{doomed}", None, 204, "api"),
    ("POST", "/cutout", "image", 200, "cutout"),
    ("POST", "/art/generate", ART, 200, "art"),
    ("GET", "/v1/models", None, 200, "llm"),
    ("POST", "/v1/chat/completions", CHAT, 200, "llm"),
    ("GET", "/gpu", None, 200, "gpu-stats"),
]
IDS = [f"{method} {path}" for method, path, *_ in ROUTES]

# Paths the stack does not serve. vLLM's open routes are among them: Caddy
# sends only /v1/* to vLLM, so /health and /metrics land on the api's gate. So
# do the vision models' own /healthz and anything near their one route.
UNKNOWN = [
    "/health",
    "/metrics",
    "/nope",
    "/v1",
    "/art",
    "/art/healthz",
    "/cutout/healthz",
    "/cards/abc123def456ghi789/photo.jpg",
]

# One cheap authenticated call per service that keeps a limiter. The two
# vision models answer 405 to a GET, after the gate has let the key through.
CLEARING_CALLS = ["/healthz", "/v1/models", "/cutout", "/art/generate", "/gpu"]


def call(
    gateway: str, method: str, path: str, key: str | None, body=None, files=None
) -> httpx.Response:
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    if files is None and isinstance(body, dict):
        return httpx.request(method, f"{gateway}{path}", headers=headers, json=body, timeout=30)
    return httpx.request(
        method, f"{gateway}{path}", headers=headers, data=body, files=files, timeout=30
    )


def make_card(gateway: str, key: str, portrait) -> str:
    response = call(gateway, "POST", "/cards", key, {"stats": '{"name": "Sam"}'}, photo(portrait))
    assert response.status_code == 202, response.text
    card_id = response.json()["card_id"]
    for _ in range(240):
        job = call(gateway, "GET", f"/cards/{card_id}", key).json()
        if job["status"] in ("done", "failed"):
            return card_id
        time.sleep(0.25)
    raise AssertionError(f"card {card_id} did not finish")


@pytest.fixture(scope="module")
def card(gateway: str, live_key: str, portrait) -> str:
    card_id = make_card(gateway, live_key, portrait)
    yield card_id
    call(gateway, "DELETE", f"/cards/{card_id}", live_key)


def request_for(route, card: str, doomed: str, portrait) -> tuple[str, str, object, object]:
    method, path, body, _, _ = route
    path = path.format(card=card, doomed=doomed)
    files = None
    if body == "card":
        body, files = {"stats": '{"name": "Sam"}'}, photo(portrait)
    elif body == "image":
        body, files = None, {"image": ("me.jpg", portrait.read_bytes(), "image/jpeg")}
    return method, path, body, files


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


# The page and its files, the other open paths. The key is typed into the page,
# so the page cannot need it.
PAGE = [
    "/",
    "/app/app.js",
    "/app/app.css",
    "/app/brand/akamai-logo-white.svg",
    "/vendor/tasks-vision/vision_bundle.mjs",
]


@pytest.mark.parametrize("path", PAGE)
def test_the_page_and_its_files_are_open(gateway: str, path: str) -> None:
    assert httpx.get(f"{gateway}{path}", timeout=10).status_code == 200, path


def test_a_file_the_page_does_not_have_is_a_404_not_a_401(gateway: str) -> None:
    for path in ("/app/nope.js", "/vendor/nope.wasm"):
        assert httpx.get(f"{gateway}{path}", timeout=10).status_code == 404, path


@pytest.mark.parametrize("route", ROUTES, ids=IDS)
def test_no_header_is_401(gateway: str, card: str, portrait, route) -> None:
    method, path, body, files = request_for(route, card, card, portrait)
    response = call(gateway, method, path, None, body, files)
    assert response.status_code == 401, f"{method} {path} answered {response.status_code}"


@pytest.mark.parametrize("route", ROUTES, ids=IDS)
def test_wrong_key_is_401(gateway: str, card: str, portrait, route) -> None:
    method, path, body, files = request_for(route, card, card, portrait)
    response = call(gateway, method, path, WRONG_KEY, body, files)
    assert response.status_code == 401, f"{method} {path} answered {response.status_code}"


@pytest.mark.parametrize("route", ROUTES, ids=IDS)
def test_right_key_is_the_routes_own_answer(
    gateway: str, live_key: str, card: str, portrait, route
) -> None:
    doomed = make_card(gateway, live_key, portrait) if "{doomed}" in route[1] else card
    method, path, body, files = request_for(route, card, doomed, portrait)
    response = call(gateway, method, path, live_key, body, files)
    assert response.status_code == route[3], f"{method} {path} answered {response.status_code}"
    if path == "/cards":
        call(gateway, "DELETE", f"/cards/{response.json()['card_id']}", live_key)


@pytest.mark.parametrize("path", UNKNOWN)
def test_a_path_the_stack_does_not_serve_fails_closed(
    gateway: str, live_key: str, path: str
) -> None:
    assert call(gateway, "GET", path, None).status_code == 401, path
    assert call(gateway, "GET", path, WRONG_KEY).status_code == 401, path
    assert call(gateway, "GET", path, live_key).status_code == 404, path


@pytest.mark.parametrize(
    "path",
    [
        "/livez/../healthz",
        "/v1/../healthz",
        "/v1/../../models",
        "/app/../healthz",
        "/vendor/../models",
    ],
)
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
