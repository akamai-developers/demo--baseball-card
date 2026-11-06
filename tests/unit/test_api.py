"""The api in process: its open route, its keyed routes, its health fan-out, /models."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from api.catalog import CatalogError
from api.health import probe_all
from tests.conftest import TEST_KEY

AUTH = {"Authorization": f"Bearer {TEST_KEY}"}


@pytest.fixture
def client(clean_settings) -> TestClient:
    from api.main import app

    with TestClient(app) as client:
        yield client


def test_livez_is_the_one_open_route(client: TestClient) -> None:
    response = client.get("/livez")
    assert (response.status_code, response.text) == (200, "ok")
    for path in ("/healthz", "/models", "/nope", "/"):
        response = client.get(path)
        assert response.status_code == 401, path
        assert response.headers["WWW-Authenticate"] == "Bearer"


def test_a_path_that_does_not_exist_is_401_without_the_key_and_404_with_it(
    client: TestClient,
) -> None:
    assert client.get("/cards/abc").status_code == 401
    assert client.get("/cards/abc", headers=AUTH).status_code == 404


def test_healthz_with_nothing_configured_is_ok(client: TestClient) -> None:
    body = client.get("/healthz", headers=AUTH).json()
    assert body["ok"] is True
    assert (body["service"], body["backend"], body["services"]) == ("api", "mock", {})


def test_models_reads_the_pins(client: TestClient) -> None:
    from tests.conftest import REPO_ROOT

    pins = (REPO_ROOT / "deploy" / "models.env").read_text()
    models = client.get("/models", headers=AUTH).json()["models"]
    assert [m["step"] for m in models] == ["cutout", "art", "text"]
    assert [m["service"] for m in models] == ["cutout", "art", "llm"]
    for entry in models:
        assert f"={entry['repo']}\n" in pins
        assert f"={entry['revision']}\n" in pins
        assert entry["url"] == f"https://huggingface.co/{entry['repo']}/tree/{entry['revision']}"
    assert [m["license"] for m in models] == ["MIT", "Apache 2.0", "Apache 2.0"]


def test_a_missing_models_env_stops_the_api_with_its_name(
    clean_settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from api.main import app
    from common.settings import get_settings

    monkeypatch.setenv("MODELS_ENV", str(tmp_path / "models.env"))
    get_settings.cache_clear()
    with pytest.raises(CatalogError, match="models.env"), TestClient(app):
        pass


def test_a_pin_that_is_missing_is_named(
    clean_settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from api.catalog import build
    from common.settings import get_settings

    pins = tmp_path / "models.env"
    pins.write_text("CUTOUT_MODEL=a/b\nCUTOUT_REVISION=1\nCUTOUT_LICENSE=MIT\n")
    monkeypatch.setenv("MODELS_ENV", str(pins))
    get_settings.cache_clear()
    with pytest.raises(CatalogError, match="ART_MODEL"):
        build(get_settings())


# -- the fan-out -------------------------------------------------------------


def transport(answers: dict[str, httpx.Response | Exception]) -> httpx.MockTransport:
    """Answer by host, and check that every probe carries the key."""

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {TEST_KEY}"
        answer = answers[request.url.host]
        if isinstance(answer, Exception):
            raise answer
        return answer

    return httpx.MockTransport(handle)


async def probe_with(monkeypatch: pytest.MonkeyPatch, answers: dict, urls: dict[str, str]) -> dict:
    real_client = httpx.AsyncClient

    def client(**kwargs) -> httpx.AsyncClient:
        return real_client(transport=transport(answers), **kwargs)

    monkeypatch.setattr("api.health.httpx.AsyncClient", client)
    return await probe_all(urls, TEST_KEY, llm_ran_on="mock-gpu0")


async def test_vllm_is_asked_for_its_model_list(monkeypatch: pytest.MonkeyPatch) -> None:
    listed = {"data": [{"id": "google/gemma", "owned_by": "vllm"}]}
    found = await probe_with(
        monkeypatch, {"llm": httpx.Response(200, json=listed)}, {"llm": "http://llm:8000"}
    )
    health = found["llm"]
    assert (health.status, health.model, health.backend) == ("ok", "google/gemma", "vllm")
    assert (health.loaded, health.ran_on) == (True, "mock-gpu0")


async def test_a_service_reports_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {
        "status": "loading",
        "model": "ZhengPeng7/BiRefNet",
        "backend": "real",
        "device": "cuda",
        "loaded": False,
        "ran_on": "akamai-gpu0",
        "vram_mb": 2012,
    }
    found = await probe_with(
        monkeypatch, {"cutout": httpx.Response(200, json=body)}, {"cutout": "http://cutout:8001"}
    )
    health = found["cutout"]
    assert (health.status, health.backend, health.ran_on, health.vram_mb) == (
        "loading",
        "real",
        "akamai-gpu0",
        2012,
    )


async def test_a_service_that_errors_or_is_gone_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = {
        "cutout": httpx.Response(500),
        "art": httpx.ConnectError("refused"),
        "llm": httpx.Response(200, json={"data": []}),
    }
    urls = {"cutout": "http://cutout:8001", "art": "http://art:8002", "llm": "http://llm:8000"}
    found = await probe_with(monkeypatch, answers, urls)
    assert (found["cutout"].status, found["cutout"].error) == ("error", "HTTP 500")
    assert (found["art"].status, found["art"].error) == ("unreachable", "ConnectError")
    assert found["llm"].status == "loading"


def test_healthz_is_not_ok_while_one_service_is_not(
    clean_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api.main import app
    from common.schemas import ServiceHealth
    from common.settings import get_settings

    monkeypatch.setenv("LLM_URL", "http://llm:8000")
    get_settings.cache_clear()

    async def fake(urls, key, llm_ran_on=None):
        return {name: ServiceHealth(status="loading") for name in urls}

    monkeypatch.setattr("api.main.probe_all", fake)
    with TestClient(app) as client:
        body = client.get("/healthz", headers=AUTH).json()
    assert body["ok"] is False
    assert body["services"]["llm"]["status"] == "loading"
