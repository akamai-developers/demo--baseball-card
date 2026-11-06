"""The key travels in a header. No log may ever contain it.

Two places are read: every container log the mock stack writes, after traffic
that carries the key through every service, and every file under build-logs/,
where this repo's build runner keeps the output of each of its runs.
"""

from __future__ import annotations

import subprocess
import time

import httpx
import pytest

from tests.conftest import REPO_ROOT, dotenv_value, refute_key


def refute_header(logs: str) -> None:
    """Fail when a log line names the Authorization header. The line would carry
    the key after the name, so the failure says where and prints no line."""
    if "authorization" in logs.lower():
        raise AssertionError("a log line names the Authorization header")


def compose_logs(docker: list[str]) -> str:
    result = subprocess.run(
        [*docker, "compose", "--profile", "mock", "logs", "--no-color", "--tail", "2000"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.stdout + result.stderr


def make_a_card(gateway: str, headers: dict, portrait) -> None:
    """A whole card: the api calls cutout, art and the llm, each with the key."""
    created = httpx.post(
        f"{gateway}/cards",
        headers=headers,
        files={"photo": ("me.jpg", portrait.read_bytes(), "image/jpeg")},
        data={"stats": '{"name": "Sam Rivera", "metrics": {"exit_velo_mph": 88.5}}'},
        timeout=30,
    )
    assert created.status_code == 202, created.text
    card_id = created.json()["card_id"]
    for _ in range(240):
        job = httpx.get(f"{gateway}/cards/{card_id}", headers=headers, timeout=10).json()
        if job["status"] in ("done", "failed"):
            break
        time.sleep(0.25)
    httpx.delete(f"{gateway}/cards/{card_id}", headers=headers, timeout=30)


@pytest.mark.integration
def test_the_key_never_reaches_a_log(
    docker: list[str], gateway: str, live_key: str, portrait
) -> None:
    """Send the key through every service, then read every log the stack wrote.

    A card and /healthz make the api call each model service with the key,
    which is the traffic most likely to leak it.
    """
    headers = {"Authorization": f"Bearer {live_key}"}
    httpx.get(f"{gateway}/healthz", headers=headers, timeout=10)
    httpx.get(f"{gateway}/healthz", timeout=10)
    httpx.get(f"{gateway}/nope", headers=headers, timeout=10)
    httpx.get(f"{gateway}/models", headers={"Authorization": f"Bearer {live_key}x"}, timeout=10)
    httpx.post(
        f"{gateway}/v1/chat/completions",
        headers=headers,
        json={"messages": [{"role": "user", "content": "hello"}]},
        timeout=30,
    )
    httpx.post(
        f"{gateway}/cutout",
        headers=headers,
        files={"image": ("me.jpg", portrait.read_bytes(), "image/jpeg")},
        timeout=60,
    )
    httpx.post(
        f"{gateway}/art/generate",
        headers=headers,
        json={"prompt": "a stadium", "width": 256, "height": 352},
        timeout=60,
    )
    httpx.get(f"{gateway}/gpu", headers=headers, timeout=10)
    make_a_card(gateway, headers, portrait)

    logs = compose_logs(docker)
    assert "GET /healthz 200" in logs, "the access log is not there, so this proved nothing"
    for line in ("POST /cutout 200", "POST /art/generate 200", "GET /gpu 200", "POST /cards 202"):
        assert line in logs, f"no {line} in the logs, so this proved less than it says"
    refute_key(logs, live_key, "the stack's logs")
    refute_header(logs)


@pytest.mark.integration
def test_no_service_prints_its_own_key_on_start(docker: list[str], gateway: str) -> None:
    """The startup guard reports a short key by length, never by value."""
    logs = compose_logs(docker)
    assert logs.strip(), "no logs came back, so this proved nothing"
    key = dotenv_value("CARD_API_KEY") or ""
    refute_key(logs, key[:16], "the stack's logs, the first 16 characters of the key")


def test_the_gateway_log_deletes_headers() -> None:
    """Caddy's default JSON log prints request headers. Ours must not."""
    caddyfile = (REPO_ROOT / "Caddyfile").read_text()
    # Twice: the access log, and the default logger every other message goes
    # through, the reverse proxy's warnings included.
    assert caddyfile.count("request>headers delete") == 2
    assert caddyfile.count("resp_headers delete") == 2
    assert "log default {" in caddyfile


def test_the_key_is_in_no_build_log() -> None:
    key = dotenv_value("CARD_API_KEY")
    if not key:
        pytest.skip("no CARD_API_KEY in .env, so there is nothing to look for")
    logs = sorted(path for path in (REPO_ROOT / "build-logs").glob("**/*") if path.is_file())
    if not logs:
        pytest.skip("no build-logs/ here. Only a box that ran the build has one")
    for path in logs:
        text = path.read_bytes().decode("utf-8", errors="replace")
        refute_key(text, key, f"build-logs/{path.relative_to(REPO_ROOT / 'build-logs')}")
