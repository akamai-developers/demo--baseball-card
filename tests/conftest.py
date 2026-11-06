"""Shared fixtures.

Unit tests run anywhere. Integration and security tests that need containers
skip themselves with a readable reason when Docker or the mock stack is not up.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_KEY = "t" * 64


@pytest.fixture
def api_key() -> str:
    """A key long enough to pass the startup guard. Not a real one."""
    return TEST_KEY


@pytest.fixture
def clean_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Give a test its own Settings, built from env vars and a throwaway data dir.

    The environment wins over the .env at the repo root, so every value this
    box's .env could hold that a test depends on is set here.
    """
    from common.settings import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("CARD_API_KEY", TEST_KEY)
    monkeypatch.setenv("BACKEND", "mock")
    monkeypatch.setenv("SERVICE_NAME", "api")
    monkeypatch.setenv("GPU_LABEL", "mock")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    for name in ("CUTOUT_URL", "ART_URL", "LLM_URL", "LLM_MODEL", "LLM_LICENSE", "LLM_RAN_ON"):
        monkeypatch.setenv(name, "")
    yield get_settings()
    get_settings.cache_clear()


def dotenv_value(name: str) -> str | None:
    """Read one value out of the repo .env without importing anything."""
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return None
    value = None
    for line in env_path.read_text().splitlines():
        if line.startswith(f"{name}="):
            value = line.split("=", 1)[1].strip()
    return value


def docker_cmd() -> list[str] | None:
    """The docker command the Makefile uses, from DOCKER, or None when it does not work.

    `make test` exports DOCKER. Where docker needs sudo, DOCKER="sudo -E docker":
    -E keeps the variables the compose tests set, which plain sudo drops.
    """
    cmd = shlex.split(os.environ.get("DOCKER", "docker"))
    if not cmd or shutil.which(cmd[0]) is None:
        return None
    quiet = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "timeout": 30}
    try:
        works = subprocess.run([*cmd, "info"], **quiet).returncode == 0
    except subprocess.TimeoutExpired:
        works = False
    return cmd if works else None


@pytest.fixture(scope="session")
def docker() -> list[str]:
    cmd = docker_cmd()
    if cmd is None:
        pytest.skip('docker does not answer. Where it needs sudo, set DOCKER="sudo -E docker"')
    return cmd


class Secret(str):
    """A str that keeps its value out of tracebacks.

    A failing test's traceback lists the test's arguments with repr(), and one
    of them is the key the stack runs with. On a laptop that shares its .env
    with a node, that is the node's key. It works as the str it is everywhere
    else: in a header, in a comparison, in `in`.
    """

    def __repr__(self) -> str:
        return "<CARD_API_KEY>"


def refute_key(text: str | None, key: str, where: str) -> None:
    """Fail when `key` is anywhere in `text`, and say where without printing either.

    A plain assert prints both sides when it fails, and this key may be a live
    node's. There is no assert in here, so pytest has nothing to spell out.
    """
    if key and key in (text or ""):
        raise AssertionError(f"the key is in {where}")


def confirm_key(value: object, key: str, where: str) -> None:
    """Fail when `value` is not `key`, without printing either."""
    if value != key:
        raise AssertionError(f"{where} does not hold the key")


@pytest.fixture(scope="session")
def live_key() -> str:
    """The key the running stack was started with."""
    key = dotenv_value("CARD_API_KEY")
    if not key:
        pytest.skip("no CARD_API_KEY in .env. run: make env")
    return Secret(key)


def gateway_url() -> str:
    """GATEWAY, else localhost on GATEWAY_PORT from the environment or .env, else 8443."""
    if url := os.environ.get("GATEWAY"):
        return url.rstrip("/")
    port = os.environ.get("GATEWAY_PORT") or dotenv_value("GATEWAY_PORT") or "8443"
    return f"http://localhost:{port}"


@pytest.fixture(scope="session")
def gateway(live_key: str) -> str:
    """The Caddy gateway of this repo's mock stack. Skips the test if it is not up.

    It has to take this repo's key. Anything else at that address is another
    stack, and a test that sends it wrong keys on purpose would trip its limit.
    """
    url = gateway_url()
    try:
        httpx.get(f"{url}/livez", timeout=3.0).raise_for_status()
        health = httpx.get(
            f"{url}/healthz", headers={"Authorization": f"Bearer {live_key}"}, timeout=10.0
        )
    except httpx.HTTPError:
        pytest.skip(f"no gateway at {url}. run: make up-mock")
    if health.status_code != 200:
        pytest.skip(f"the gateway at {url} does not take this repo's key. Is it another stack?")
    return url
