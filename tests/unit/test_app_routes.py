"""The page and its files, served by the api without the key, and nothing more.

In process, through the api's own app object. The same paths through Caddy are
in tests/security/test_gateway_auth.py.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

KEY = "t" * 64
AUTH = {"Authorization": f"Bearer {KEY}"}


@pytest.fixture
def client(clean_settings) -> TestClient:
    from api.main import app

    with TestClient(app) as test_client:
        yield test_client


def test_the_page_is_served_without_a_key(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    # The page is asked for again on every load, so a deploy is seen at once.
    assert response.headers["cache-control"] == "no-store"
    assert '<script type="module" src="/app/app.js"></script>' in response.text


@pytest.mark.parametrize(
    "path",
    [
        "/app/app.js",
        "/app/app.css",
        "/app/brand/akamai-logo-white.svg",
        "/app/fonts/InstrumentSans.ttf",
        "/app/styles/akamai.jpg",
        "/app/styles/classic.jpg",
    ],
)
def test_the_files_are_served_without_a_key_and_checked_on_every_load(
    client: TestClient, path: str
) -> None:
    response = client.get(path)
    assert response.status_code == 200, path
    assert response.headers["cache-control"] == "no-cache"
    # With the ETag, a file that did not change is a 304 of a few bytes.
    again = client.get(path, headers={"If-None-Match": response.headers["etag"]})
    assert again.status_code == 304


def test_a_missing_file_is_a_404_not_a_401(client: TestClient) -> None:
    assert client.get("/app/nope.css").status_code == 404


def test_the_vendor_mount_answers_even_when_it_is_empty(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """web/vendor is gitignored, so a fresh clone has no such folder until `make
    web-vendor` runs. An empty folder stands in for it here. The page has to get
    a 404 rather than a 401, so detector.js can turn the framing guide off."""
    from api.main import app

    vendor = next(route.app for route in app.routes if getattr(route, "name", None) == "vendor")
    monkeypatch.setattr(vendor, "directory", tmp_path)
    monkeypatch.setattr(vendor, "all_directories", [tmp_path])
    monkeypatch.setattr(vendor, "config_checked", False)
    assert client.get("/vendor/tasks-vision/vision_bundle.mjs").status_code == 404


@pytest.mark.parametrize("path", ["/healthz", "/models", "/cards/abc", "/index.html", "/app"])
def test_everything_else_still_needs_the_key(client: TestClient, path: str) -> None:
    """/app with no slash is not under /app/, and /index.html is not /."""
    assert client.get(path).status_code == 401, path


def test_a_card_route_answers_with_the_key(client: TestClient) -> None:
    """The page's calls are the api's routes, and the key still opens them."""
    assert client.get("/models", headers=AUTH).status_code == 200
