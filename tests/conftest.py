"""Shared fixtures.

Unit tests run anywhere. Integration and security tests that need containers
skip themselves with a readable reason when Docker or the mock stack is not up.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_KEY = "t" * 64


def load_script(path: Path) -> ModuleType:
    """A script that is not in a package, such as deploy/scripts/gpu_check.py,
    imported by its path so a test can call its functions."""
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    # A dataclass looks its module up here while the class is being made.
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)
    return module


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
    # No pool, so an art service started in process paints every request and
    # starts no painter. The pool's tests turn it on.
    monkeypatch.setenv("ART_POOL_SIZE", "0")
    for name in ("CUTOUT_URL", "ART_URL", "LLM_URL", "LLM_MODEL", "LLM_LICENSE", "LLM_RAN_ON"):
        monkeypatch.setenv(name, "")
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture(scope="session")
def portrait() -> Path:
    """The CC0 fixture photo. tests/fixtures/SOURCES.md has the licence."""
    return REPO_ROOT / "tests" / "fixtures" / "portrait.jpg"


@pytest.fixture(scope="session")
def example_stats() -> str:
    """examples/stats.json as text, which is what a caller posts."""
    return (REPO_ROOT / "examples" / "stats.json").read_text()


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


def compose_config(
    docker: list[str],
    profiles: tuple[str, ...],
    env: dict[str, str] | None = None,
    files: tuple[str, ...] = (),
) -> dict:
    """`docker compose config` for these profiles, as a dict.

    Compose reads CARD_API_KEY when it starts, for vLLM's key. A clone that has
    not run `make env` has none, so a stand-in is passed when neither the shell
    nor .env holds one. The rendered config holds the key from .env: compare
    names, or use refute_key, and never print it.
    """
    environ = dict(os.environ)
    if not environ.get("CARD_API_KEY") and not dotenv_value("CARD_API_KEY"):
        environ["CARD_API_KEY"] = TEST_KEY
    flags = [flag for name in files for flag in ("-f", name)]
    flags += [flag for name in profiles for flag in ("--profile", name)]
    result = subprocess.run(
        [*docker, "compose", *flags, "config", "--format", "json"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env={**environ, **(env or {})},
        timeout=60,
    )
    if result.returncode != 0:
        pytest.fail(f"docker compose config failed:\n{result.stderr}")
    return json.loads(result.stdout)


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


@pytest.fixture(scope="session")
def mock_backends(gateway: str, live_key: str) -> None:
    """Skip a test that only makes sense when every service in the stack is a mock.

    A stack started with a real model in it would fail the mock-only
    assertions with "akamai-cpu is not mock", which reads like a bug and is
    not one.
    """
    health = httpx.get(
        f"{gateway}/healthz", headers={"Authorization": f"Bearer {live_key}"}, timeout=10
    ).json()
    real = [
        name
        for name, svc in health.get("services", {}).items()
        if svc.get("backend") not in (None, "mock", "vllm")
    ]
    if real:
        pytest.skip(f"{', '.join(real)} is running a real backend. run: make down && make up-mock")
