"""The mock stack, reached the way a user reaches it: through Caddy.

Skips itself when the stack is not up. Start it with: make up-mock
"""

from __future__ import annotations

import httpx
import pytest

pytestmark = pytest.mark.integration


def test_livez_needs_no_key(gateway: str) -> None:
    response = httpx.get(f"{gateway}/livez", timeout=10)
    assert response.status_code == 200
    assert response.text.strip() == "ok"


def test_healthz_needs_the_key(gateway: str, live_key: str) -> None:
    assert httpx.get(f"{gateway}/healthz", timeout=10).status_code == 401
    response = httpx.get(
        f"{gateway}/healthz", headers={"Authorization": f"Bearer {live_key}"}, timeout=10
    )
    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "api"
    assert "version" in body
    assert "services" in body


def test_an_unknown_path_does_not_leak_the_route_list(gateway: str) -> None:
    response = httpx.get(f"{gateway}/admin", timeout=10)
    assert response.status_code in (401, 404)
    assert "cards" not in response.text


def test_the_gateway_does_not_expose_caddy_admin(gateway: str) -> None:
    """Caddy's admin API is off. Port 2019 must refuse the connection."""
    with pytest.raises(httpx.HTTPError):
        httpx.get("http://localhost:2019/config/", timeout=3)
